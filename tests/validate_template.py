#!/usr/bin/env python3
"""Validate the AMP template and extract its executable stages for CI."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
SCRIPT_FILES = ("postgresqltlsupdates.json", "postgresqltlsstart.json")
SCRIPT_NAMES = {
    "Build checksum-verified PostgreSQL with OpenSSL": "build.sh",
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
            if stage.get("UpdateSourceData") != "/bin/bash":
                continue
            name = stage["UpdateStageName"]
            wrapper = stage["UpdateSourceArgs"]
            if not (wrapper.startswith('-c "') and wrapper.endswith('"')):
                raise AssertionError(f"unexpected bash wrapper: {name}")
            body = wrapper[4:-1].replace(r'\"', '"')
            if name not in SCRIPT_NAMES:
                raise AssertionError(f"unrecognised executable stage: {name}")
            if any(token in body for token in USER_TOKENS):
                raise AssertionError(f"AMP setting interpolated into shell source: {name}")
            scripts[SCRIPT_NAMES[name]] = body
    if set(scripts) != set(SCRIPT_NAMES.values()):
        raise AssertionError("one or more expected executable stages are missing")
    return scripts


def find_bash() -> str:
    candidates = [
        os.environ.get("BASH"),
        shutil.which("bash"),
        r"C:\Program Files\Git\bin\bash.exe",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return candidate
    raise AssertionError("bash is required for shell syntax tests")


def validate_kvp() -> None:
    keys: set[str] = set()
    for line in (ROOT / "postgresqltls.kvp").read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#"):
            continue
        key, separator, _ = line.partition("=")
        if not separator:
            raise AssertionError(f"invalid KVP line: {line}")
        if key in keys:
            raise AssertionError(f"duplicate KVP key: {key}")
        keys.add(key)
    required = {
        "Meta.ConfigManifest",
        "Meta.MetaConfigManifest",
        "App.Ports",
        "App.UpdateSources",
        "App.PreStartStages",
        "App.EnvironmentVariables",
    }
    if not required.issubset(keys):
        raise AssertionError("required KVP entries are missing")


def validate_adversarial_inputs(bash: str, scripts: dict[str, str]) -> None:
    hostile = "'; touch \"$HM_MARKER\"; # $(touch \"$HM_MARKER\") `touch \"$HM_MARKER\"`\n"
    cases = [
        ("build.sh", "HM_PG_VERSION"),
        ("build.sh", "HM_PG_RELEASE"),
        ("build.sh", "HM_PG_SOURCE_SHA256"),
        ("initialize.sh", "HM_PG_ALLOWED_DATABASE"),
        ("initialize.sh", "HM_PG_ALLOWED_ROLE"),
        ("initialize.sh", "HM_PG_ALLOWED_IPV4_CIDR"),
        ("initialize.sh", "HM_PG_ALLOWED_IPV6_CIDR"),
        ("start.sh", "HM_PG_LISTEN_ADDRESS"),
    ]
    with tempfile.TemporaryDirectory(prefix="amp-pg-template-") as temp:
        base = Path(temp)
        (base / "data").mkdir()
        (base / "run").mkdir()
        (base / "tls").mkdir()
        marker = base / "injected"
        defaults = {
            "HM_PG_BASE_DIR": base.as_posix() + "/",
            "HM_PG_LISTEN_ADDRESS": "127.0.0.1",
            "HM_PG_RELEASE": "16",
            "HM_PG_VERSION": "16.14",
            "HM_PG_SOURCE_SHA256": "a" * 64,
            "HM_PG_ALLOWED_DATABASE": "REPLACE_BEFORE_REMOTE_USE",
            "HM_PG_ALLOWED_ROLE": "REPLACE_BEFORE_REMOTE_USE",
            "HM_PG_ALLOWED_IPV4_CIDR": "127.0.0.1/32",
            "HM_PG_ALLOWED_IPV6_CIDR": "::1/128",
            "HM_PG_TLS_HOSTNAME": "REPLACE_BEFORE_TLS_USE",
            "PGPORT": "55432",
            "HM_MARKER": marker.as_posix(),
        }
        for script_name, variable in cases:
            environment = os.environ.copy()
            environment.update(defaults)
            environment[variable] = hostile
            result = subprocess.run(
                [bash, "-c", scripts[script_name]],
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=10,
                check=False,
            )
            if result.returncode == 0:
                raise AssertionError(f"hostile {variable} unexpectedly succeeded")
            if marker.exists():
                raise AssertionError(f"shell injection executed through {variable}")

        (base / "tls" / "server.crt").write_text("not a certificate\n", encoding="utf-8")
        (base / "tls" / "server.key").write_text("not a private key\n", encoding="utf-8")
        environment = os.environ.copy()
        environment.update(defaults)
        environment["HM_PG_TLS_HOSTNAME"] = hostile
        result = subprocess.run(
            [bash, "-c", scripts["start.sh"]],
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=10,
            check=False,
        )
        if result.returncode == 0:
            raise AssertionError("hostile HM_PG_TLS_HOSTNAME unexpectedly succeeded")
        if marker.exists():
            raise AssertionError("shell injection executed through HM_PG_TLS_HOSTNAME")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--extract-dir", type=Path)
    args = parser.parse_args()

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
        subprocess.run([bash, "-n", "-c", body], check=True)
    validate_adversarial_inputs(bash, scripts)

    if args.extract_dir:
        args.extract_dir.mkdir(parents=True, exist_ok=True)
        for name, body in scripts.items():
            target = args.extract_dir / name
            target.write_text("#!/usr/bin/env bash\n" + body + "\n", encoding="utf-8", newline="\n")
            target.chmod(0o755)

    print("Template JSON, KVP, shell syntax and injection regression tests passed")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (AssertionError, json.JSONDecodeError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
        print(f"validation failed: {error}", file=sys.stderr)
        sys.exit(1)
