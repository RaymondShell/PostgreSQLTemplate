#!/usr/bin/env python3
"""Validate the AMP template and extract its executable stages for CI."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
SCRIPT_FILES = ("postgresqltlsupdates.json", "postgresqltlsstart.json")
SCRIPT_NAMES = {
    "Build checksum-verified PostgreSQL with OpenSSL": "build.sh",
    "Install deterministic PostgreSQL HBA renderer": "render-hba.sh",
    "Stage PostgreSQL lifecycle supervisor": "supervise.sh",
    "Initialize PostgreSQL and enforce TLS-only remote HBA": "initialize.sh",
    "Run PostgreSQL with fail-closed TLS selection": "start.sh",
    "Verify exact PostgreSQL instance and TLS state": "verify.sh",
}
USER_TOKENS = {
    "{{ServerVersion}}",
    "{{CustomServerVersion}}",
    "{{PostgreSQLSourceSha256}}",
    "{{AllowedDatabase}}",
    "{{AllowedRole}}",
    "{{AllowedIPv4CIDR}}",
    "{{AllowedIPv6CIDR}}",
    "{{AdminDatabases}}",
    "{{AdminRole}}",
    "{{AdminIPv4CIDR}}",
    "{{AdminIPv6CIDR}}",
    "{{CommercialDatabase}}",
    "{{CommercialRuntimeRole}}",
    "{{CommercialAuthorizerRole}}",
    "{{CommercialMigratorRole}}",
    "{{CommercialServiceIPv4CIDR}}",
    "{{CommercialServiceIPv6CIDR}}",
    "{{CommercialMigrationIPv4CIDR}}",
    "{{CommercialMigrationIPv6CIDR}}",
    "{{TLSHostname}}",
    "{{$ApplicationIPBinding}}",
}


def load_json(name: str):
    with (ROOT / name).open(encoding="utf-8") as handle:
        return json.load(handle)


def extract_scripts() -> dict[str, str]:
    scripts: dict[str, str] = {}
    for filename in SCRIPT_FILES:
        for stage in load_json(filename):
            name = stage["UpdateStageName"]
            if name in SCRIPT_NAMES and stage.get("UpdateSource") == "CreateFile":
                body = stage["UpdateSourceData"]
                if any(token in body for token in USER_TOKENS):
                    raise AssertionError(f"AMP setting interpolated into installed script: {name}")
                scripts[SCRIPT_NAMES[name]] = body
                continue
            if stage.get("UpdateSourceData") != "/bin/bash":
                continue
            if name == "Regenerate deterministic PostgreSQL HBA before start":
                if stage.get("UpdateSourceArgs") != "{{$FullBaseDir}}render-hba.sh":
                    raise AssertionError("HBA regeneration is not a direct fixed-path invocation")
                continue
            wrapper = stage["UpdateSourceArgs"]
            if "\\\\" in wrapper:
                raise AssertionError(
                    f"AMP bash wrapper contains a doubled backslash: {name}"
                )
            argv = shlex.split(wrapper, posix=True)
            if len(argv) != 2 or argv[0] != "-c":
                raise AssertionError(f"unexpected bash wrapper: {name}")
            body = argv[1]
            if name not in SCRIPT_NAMES:
                raise AssertionError(f"unrecognised executable stage: {name}")
            if any(token in body for token in USER_TOKENS):
                raise AssertionError(f"AMP setting interpolated into shell source: {name}")
            if "HM_PG_" in body:
                raise AssertionError(f"runtime-only environment used by AMP stage: {name}")
            scripts[SCRIPT_NAMES[name]] = body
    if set(scripts) != set(SCRIPT_NAMES.values()):
        raise AssertionError("one or more expected executable stages are missing")
    return scripts


def find_bash() -> str:
    candidates = (
        [r"C:\Program Files\Git\bin\bash.exe", os.environ.get("BASH"), shutil.which("bash")]
        if os.name == "nt"
        else [os.environ.get("BASH"), shutil.which("bash")]
    )
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return candidate
    raise AssertionError("bash is required for shell syntax tests")


def validate_kvp() -> None:
    values: dict[str, str] = {}
    for line in (ROOT / "postgresqltls.kvp").read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if not separator:
            raise AssertionError(f"invalid KVP line: {line}")
        if key in values:
            raise AssertionError(f"duplicate KVP key: {key}")
        values[key] = value
    required = {
        "Meta.ConfigManifest",
        "Meta.MetaConfigManifest",
        "App.Ports",
        "App.UpdateSources",
        "App.PreStartStages",
        "App.EnvironmentVariables",
    }
    if not required.issubset(values):
        raise AssertionError("required KVP entries are missing")
    expected_filter = (
        r"\e\[(\d+;)*(\d+)?[ABCDHJKfmsu]|\e\[?[?\>\=\da-z]+"
    )
    if values.get("Console.FilterMatchRegex") != expected_filter:
        raise AssertionError("AMP console filter regex is invalid")
    lifecycle = {
        "App.ExecutableLinux": "supervise.sh",
        "App.LinuxCommandLineArgs": "",
        "App.CommandLineArgs": "",
        "App.ExitMethod": "SIGTERM",
        "App.ExitTimeout": "45",
        "App.HasWriteableConsole": "False",
        "App.HasReadableConsole": "True",
        "App.ApplicationReadyMode": "RegexMatch",
        "Console.AppReadyRegex": r"^AMP_POSTGRESQL_SUPERVISOR_READY$",
    }
    for key, expected in lifecycle.items():
        if values.get(key) != expected:
            raise AssertionError(f"invalid AMP supervisor lifecycle setting: {key}")
    ordered_update_stages = load_json("postgresqltlsupdates.json")
    update_stages = {
        stage["UpdateStageName"]: stage for stage in ordered_update_stages
    }
    supervisor_update_contract = (
        (
            "Remove stale staged PostgreSQL lifecycle supervisor",
            "Executable",
            "/bin/rm",
            "-f -- {{$FullBaseDir}}supervise.sh.next",
        ),
        (
            "Stage PostgreSQL lifecycle supervisor",
            "CreateFile",
            None,
            "{{$FullBaseDir}}supervise.sh.next",
        ),
        (
            "Restrict staged PostgreSQL lifecycle supervisor permissions",
            "Executable",
            "/bin/chmod",
            "0500 {{$FullBaseDir}}supervise.sh.next",
        ),
        (
            "Activate PostgreSQL lifecycle supervisor",
            "Executable",
            "/bin/mv",
            "-Tf -- {{$FullBaseDir}}supervise.sh.next {{$FullBaseDir}}supervise.sh",
        ),
    )
    update_names = [stage["UpdateStageName"] for stage in ordered_update_stages]
    contract_names = [spec[0] for spec in supervisor_update_contract]
    contract_start = update_names.index(contract_names[0])
    if update_names[contract_start : contract_start + len(contract_names)] != contract_names:
        raise AssertionError("AMP supervisor replacement stages are not contiguous and ordered")
    for name, source, executable, arguments in supervisor_update_contract:
        stage = update_stages.get(name, {})
        if (
            stage.get("UpdateSourcePlatform") != "Linux"
            or stage.get("UpdateSource") != source
            or stage.get("UpdateSourceArgs") != arguments
            or stage.get("SkipOnFailure") is not False
            or (executable is not None and stage.get("UpdateSourceData") != executable)
        ):
            raise AssertionError(f"invalid fail-closed supervisor replacement stage: {name}")
    staged_supervisor = update_stages["Stage PostgreSQL lifecycle supervisor"]
    if staged_supervisor.get("OverwriteExistingFiles") is not True:
        raise AssertionError("staged supervisor replacement is not overwrite-enabled")
    if any(
        stage.get("UpdateSourceData") == "/bin/chmod"
        and stage.get("UpdateSourceArgs") == "0500 {{$FullBaseDir}}supervise.sh"
        for stage in ordered_update_stages
    ):
        raise AssertionError("active supervisor is chmodded after atomic activation")
    ordered_start_stages = load_json("postgresqltlsstart.json")
    start_stages = {
        stage["UpdateStageName"]: stage for stage in ordered_start_stages
    }
    renderer_install = update_stages.get(
        "Install deterministic PostgreSQL HBA renderer", {}
    )
    renderer_refresh = start_stages.get(
        "Refresh deterministic PostgreSQL HBA renderer before start", {}
    )
    renderer_run = start_stages.get(
        "Regenerate deterministic PostgreSQL HBA before start", {}
    )
    if (
        renderer_refresh.get("UpdateSource") != "CreateFile"
        or renderer_refresh.get("UpdateSourceData")
        != renderer_install.get("UpdateSourceData")
        or renderer_refresh.get("UpdateSourceArgs")
        != "{{$FullBaseDir}}render-hba.sh"
        or renderer_refresh.get("OverwriteExistingFiles") is not True
        or renderer_refresh.get("SkipOnFailure") is not False
    ):
        raise AssertionError("PreStart does not refresh the reviewed HBA renderer")
    if (
        renderer_run.get("UpdateSourceData") != "/bin/bash"
        or renderer_run.get("UpdateSourceArgs")
        != "{{$FullBaseDir}}render-hba.sh"
        or renderer_run.get("SkipOnFailure") is not False
        or "-c" in renderer_run.get("UpdateSourceArgs", "")
    ):
        raise AssertionError("HBA regeneration still depends on nested shell parsing")
    start_names = [stage["UpdateStageName"] for stage in ordered_start_stages]
    if start_names.index(
        "Refresh deterministic PostgreSQL HBA renderer before start"
    ) >= start_names.index("Regenerate deterministic PostgreSQL HBA before start"):
        raise AssertionError("HBA renderer is executed before its reviewed refresh")
    renderer_body = renderer_install.get("UpdateSourceData", "")
    if (
        "BASH_SOURCE[0]" not in renderer_body
        or 'Settings="${ScriptDirectory}/settings"' not in renderer_body
        or "HBA renderer path does not match" not in renderer_body
        or 'Base="${ResolvedBase}/"' not in renderer_body
    ):
        raise AssertionError("HBA renderer is not self-locating and path-bound")


def validate_adversarial_inputs(bash: str, scripts: dict[str, str]) -> None:
    hostile = "'; touch \"$HM_MARKER\"; # $(touch \"$HM_MARKER\") `touch \"$HM_MARKER\"`\n"
    with tempfile.TemporaryDirectory(prefix="amp-pg-template-") as temp:
        instance = Path(temp)
        base = instance / "postgresql"
        settings = base / "settings"
        settings.mkdir(parents=True)
        data = base / "data"
        data.mkdir()
        (data / "PG_VERSION").write_text("16\n", encoding="utf-8")
        (base / "run").mkdir()
        (base / "tls").mkdir()
        marker = instance / "injected"
        bash_base = base.as_posix() + "/"
        if os.name == "nt":
            bash_base = subprocess.check_output(
                [bash, "-lc", f"cygpath -u {shlex.quote(str(base))}"],
                text=True,
            ).strip() + "/"
        defaults = {
            "base-dir": bash_base,
            "port": "55432",
            "listen-address": "127.0.0.1",
            "release": "16",
            "version": "16.14",
            "source-sha256": "a" * 64,
            "allowed-database": "bazaarmanager",
            "allowed-role": "bazaarmanager",
            "allowed-ipv4-cidr": "127.0.0.1/32",
            "allowed-ipv6-cidr": "::1/128",
            "admin-databases": "postgres,huntingmacro_commercial",
            "admin-role": "pgadmin4_admin",
            "admin-ipv4-cidr": "203.0.113.10/32",
            "admin-ipv6-cidr": "::1/128",
            "commercial-database": "huntingmacro_commercial",
            "commercial-runtime-role": "hm_commercial_runtime",
            "commercial-authorizer-role": "hm_commercial_authorizer",
            "commercial-migrator-role": "hm_commercial_migrator",
            "commercial-service-ipv4-cidr": "192.0.2.20/32",
            "commercial-service-ipv6-cidr": "::1/128",
            "commercial-migration-ipv4-cidr": "192.0.2.30/32",
            "commercial-migration-ipv6-cidr": "::1/128",
            "tls-hostname": "REPLACE_BEFORE_TLS_USE",
        }
        renderer = base / "render-hba.sh"
        renderer.write_text(
            scripts["render-hba.sh"], encoding="utf-8", newline="\n"
        )
        renderer_arg = bash_base + "render-hba.sh"
        cases = [
            ("build.sh", "base-dir"),
            ("build.sh", "version"),
            ("build.sh", "release"),
            ("build.sh", "source-sha256"),
            *[("render-hba.sh", setting) for setting in (
                "allowed-database",
                "allowed-role",
                "allowed-ipv4-cidr",
                "allowed-ipv6-cidr",
                "admin-databases",
                "admin-role",
                "admin-ipv4-cidr",
                "admin-ipv6-cidr",
                "commercial-database",
                "commercial-runtime-role",
                "commercial-authorizer-role",
                "commercial-migrator-role",
                "commercial-service-ipv4-cidr",
                "commercial-service-ipv6-cidr",
                "commercial-migration-ipv4-cidr",
                "commercial-migration-ipv6-cidr",
            )],
            ("start.sh", "listen-address"),
        ]
        for script_name, setting in cases:
            for filename, value in defaults.items():
                (settings / filename).write_text(value, encoding="utf-8")
            (settings / setting).write_text(hostile, encoding="utf-8")
            environment = os.environ.copy()
            environment["HM_MARKER"] = marker.as_posix()
            command = (
                [bash, renderer_arg]
                if script_name == "render-hba.sh"
                else [bash, "-c", scripts[script_name]]
            )
            result = subprocess.run(
                command,
                cwd=instance.parent if script_name == "render-hba.sh" else instance,
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=10,
                check=False,
            )
            if result.returncode == 0:
                raise AssertionError(f"hostile {setting} unexpectedly succeeded")
            if marker.exists():
                raise AssertionError(f"shell injection executed through {setting}")

        for filename, value in defaults.items():
            (settings / filename).write_text(value, encoding="utf-8")
        (base / "tls" / "server.crt").write_text("not a certificate\n", encoding="utf-8")
        (base / "tls" / "server.key").write_text("not a private key\n", encoding="utf-8")
        (settings / "tls-hostname").write_text(hostile, encoding="utf-8")
        environment = os.environ.copy()
        environment["HM_MARKER"] = marker.as_posix()
        result = subprocess.run(
            [bash, "-c", scripts["start.sh"]],
            cwd=instance,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=10,
            check=False,
        )
        if result.returncode == 0:
            raise AssertionError("hostile tls-hostname unexpectedly succeeded")
        if marker.exists():
            raise AssertionError("shell injection executed through tls-hostname")


def validate_hba_generation(bash: str, scripts: dict[str, str]) -> None:
    with tempfile.TemporaryDirectory(prefix="amp-pg-hba-") as temp:
        instance = Path(temp)
        base = instance / "postgresql"
        settings = base / "settings"
        data = base / "data"
        settings.mkdir(parents=True)
        data.mkdir()
        (data / "PG_VERSION").write_text("16\n", encoding="utf-8")
        bash_base = base.as_posix() + "/"
        if os.name == "nt":
            bash_base = subprocess.check_output(
                [bash, "-lc", f"cygpath -u {shlex.quote(str(base))}"],
                text=True,
            ).strip() + "/"
        valid = {
            "base-dir": bash_base,
            "allowed-database": "bazaarmanager",
            "allowed-role": "bazaarmanager",
            "allowed-ipv4-cidr": "127.0.0.1/32",
            "allowed-ipv6-cidr": "::1/128",
            "admin-databases": "postgres,huntingmacro_commercial",
            "admin-role": "pgadmin4_admin",
            "admin-ipv4-cidr": "203.0.113.10/32",
            "admin-ipv6-cidr": "::1/128",
            "commercial-database": "huntingmacro_commercial",
            "commercial-runtime-role": "hm_commercial_runtime",
            "commercial-authorizer-role": "hm_commercial_authorizer",
            "commercial-migrator-role": "hm_commercial_migrator",
            "commercial-service-ipv4-cidr": "192.0.2.20/32",
            "commercial-service-ipv6-cidr": "::1/128",
            "commercial-migration-ipv4-cidr": "192.0.2.30/32",
            "commercial-migration-ipv6-cidr": "::1/128",
        }
        renderer = base / "render-hba.sh"
        renderer.write_text(
            scripts["render-hba.sh"], encoding="utf-8", newline="\n"
        )
        renderer_arg = bash_base + "render-hba.sh"

        def write_settings(values: dict[str, str]) -> None:
            for filename, value in values.items():
                (settings / filename).write_text(value, encoding="utf-8")

        def render() -> subprocess.CompletedProcess[str]:
            return subprocess.run(
                [bash, renderer_arg],
                cwd=instance.parent,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=10,
                check=False,
            )

        write_settings(valid)
        result = render()
        if result.returncode != 0:
            raise AssertionError(f"valid HBA settings failed: {result.stdout}")
        expected = [
            "local all amp peer",
            "local all all scram-sha-256",
            "hostnossl all all 0.0.0.0/0 reject",
            "hostnossl all all ::0/0 reject",
            "hostssl bazaarmanager bazaarmanager 127.0.0.1/32 scram-sha-256",
            "hostssl bazaarmanager bazaarmanager ::1/128 scram-sha-256",
            "hostssl postgres,huntingmacro_commercial pgadmin4_admin 203.0.113.10/32 scram-sha-256",
            "hostssl postgres,huntingmacro_commercial pgadmin4_admin ::1/128 scram-sha-256",
            "hostssl huntingmacro_commercial hm_commercial_runtime 192.0.2.20/32 scram-sha-256",
            "hostssl huntingmacro_commercial hm_commercial_runtime ::1/128 scram-sha-256",
            "hostssl huntingmacro_commercial hm_commercial_authorizer 192.0.2.20/32 scram-sha-256",
            "hostssl huntingmacro_commercial hm_commercial_authorizer ::1/128 scram-sha-256",
            "hostssl huntingmacro_commercial hm_commercial_migrator 192.0.2.30/32 scram-sha-256",
            "hostssl huntingmacro_commercial hm_commercial_migrator ::1/128 scram-sha-256",
        ]
        hba = data / "pg_hba.conf"
        pending = data / "pg_hba.remote.pending"
        if pending.read_text(encoding="utf-8").splitlines() != expected:
            raise AssertionError("pending HBA does not match the bounded rule set")
        if hba.read_text(encoding="utf-8").splitlines() != expected[:4]:
            raise AssertionError("preflight HBA is not local-only")
        if os.name != "nt" and (hba.stat().st_mode & 0o777) != 0o600:
            raise AssertionError("generated HBA mode is not 0600")
        if os.name != "nt" and (pending.stat().st_mode & 0o777) != 0o600:
            raise AssertionError("pending HBA mode is not 0600")

        changed = valid | {"admin-ipv4-cidr": "203.0.113.44/32"}
        write_settings(changed)
        initialize_result = subprocess.run(
            [bash, "-c", scripts["initialize.sh"]],
            cwd=instance,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=10,
            check=False,
        )
        initialized_hba = pending.read_text(encoding="utf-8")
        if initialize_result.returncode != 0:
            raise AssertionError(
                f"existing-cluster initialization failed: {initialize_result.stdout}"
            )
        if "pgadmin4_admin 203.0.113.44/32" not in initialized_hba:
            raise AssertionError("initialization did not invoke the installed renderer")

        restarted = valid | {"admin-ipv4-cidr": "203.0.113.45/32"}
        write_settings(restarted)
        restart_result = render()
        restarted_hba = pending.read_text(encoding="utf-8")
        if restart_result.returncode != 0:
            raise AssertionError(
                f"ordinary-start HBA regeneration failed: {restart_result.stdout}"
            )
        if "pgadmin4_admin 203.0.113.45/32" not in restarted_hba:
            raise AssertionError("ordinary start did not apply the changed HBA setting")
        if "pgadmin4_admin 203.0.113.44/32" in restarted_hba:
            raise AssertionError("ordinary start retained the revoked HBA source")

        invalid_cases = {
            "reserved database token": {"admin-databases": "postgres,all"},
            "AMP superuser": {"admin-role": "AmP"},
            "duplicate identities": {
                "commercial-authorizer-role": "hm_commercial_runtime"
            },
            "overlong identifier": {"commercial-database": "d" * 64},
            "invalid IPv4 address": {"admin-ipv4-cidr": "999.1.1.1/32"},
            "broad IPv4 network": {"admin-ipv4-cidr": "203.0.113.0/24"},
            "invalid IPv6 address": {"commercial-service-ipv6-cidr": "12345::1/128"},
            "broad IPv6 network": {"commercial-migration-ipv6-cidr": "2001:db8::/64"},
        }
        for label, changes in invalid_cases.items():
            values = valid | changes
            write_settings(values)
            hba.write_text("sentinel-last-known-good\n", encoding="utf-8")
            pending.write_text("sentinel-pending\n", encoding="utf-8")
            result = render()
            if result.returncode == 0:
                raise AssertionError(f"{label} unexpectedly generated an HBA file")
            if hba.read_text(encoding="utf-8") != "sentinel-last-known-good\n":
                raise AssertionError(f"{label} replaced the last-known-good HBA file")
            if pending.read_text(encoding="utf-8") != "sentinel-pending\n":
                raise AssertionError(f"{label} replaced the pending HBA file")


def validate_supervisor_lifecycle(bash: str, scripts: dict[str, str]) -> None:
    if os.name == "nt":
        return
    with tempfile.TemporaryDirectory(prefix="amp-pg-supervisor-") as temp:
        supervisor = Path(temp) / "supervise.sh"
        supervisor.write_text(scripts["supervise.sh"], encoding="utf-8", newline="\n")
        result = subprocess.run(
            [bash, str(ROOT / "tests" / "supervisor_lifecycle.sh"), str(supervisor)],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=30,
            check=False,
        )
        if result.returncode != 0:
            raise AssertionError(f"supervisor lifecycle tests failed: {result.stdout}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--extract-dir", type=Path)
    parser.add_argument("--extract-only", action="store_true")
    args = parser.parse_args()

    if args.extract_only and not args.extract_dir:
        parser.error("--extract-only requires --extract-dir")

    if args.extract_only:
        scripts = extract_scripts()
        args.extract_dir.mkdir(parents=True, exist_ok=True)
        for name, body in scripts.items():
            target = args.extract_dir / name
            target.write_text(
                "#!/usr/bin/env bash\n" + body + "\n", encoding="utf-8", newline="\n"
            )
            target.chmod(0o755)
        print("Executable stages extracted")
        return 0

    for name in (
        "manifest.json",
        "postgresqltlsconfig.json",
        "postgresqltlsmetaconfig.json",
        "postgresqltlsports.json",
        *SCRIPT_FILES,
    ):
        load_json(name)

    manifest = load_json("manifest.json")
    if manifest["repotype"] != "AppTemplates" or manifest["prefix"] != "RaymondShell":
        raise AssertionError("manifest repository identity is invalid")

    validate_kvp()
    scripts = extract_scripts()
    bash = find_bash()
    for name, body in scripts.items():
        subprocess.run([bash, "-n"], input=body, text=True, check=True)
    validate_adversarial_inputs(bash, scripts)
    validate_hba_generation(bash, scripts)
    validate_supervisor_lifecycle(bash, scripts)

    if args.extract_dir:
        args.extract_dir.mkdir(parents=True, exist_ok=True)
        for name, body in scripts.items():
            target = args.extract_dir / name
            target.write_text("#!/usr/bin/env bash\n" + body + "\n", encoding="utf-8", newline="\n")
            target.chmod(0o755)

    print("Template JSON, KVP, shell safety, HBA and lifecycle tests passed")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (AssertionError, json.JSONDecodeError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
        print(f"validation failed: {error}", file=sys.stderr)
        sys.exit(1)
