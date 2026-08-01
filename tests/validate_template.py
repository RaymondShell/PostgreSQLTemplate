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
    if values.get("App.ApplicationReadyMode") != "RegexMatch":
        raise AssertionError("AMP application readiness mode is invalid")
    expected_filter = (
        r"\e\[(\d+;)*(\d+)?[ABCDHJKfmsu]|\e\[?[?\>\=\da-z]+"
    )
    if values.get("Console.FilterMatchRegex") != expected_filter:
        raise AssertionError("AMP console filter regex is invalid")
    if values.get("Console.AppReadyRegex") != r'^Type "help" for help\.$':
        raise AssertionError("AMP psql readiness regex is invalid")


def validate_adversarial_inputs(bash: str, scripts: dict[str, str]) -> None:
    hostile = "'; touch \"$HM_MARKER\"; # $(touch \"$HM_MARKER\") `touch \"$HM_MARKER\"`\n"
    cases = [
        ("build.sh", "base-dir"),
        ("build.sh", "version"),
        ("build.sh", "release"),
        ("build.sh", "source-sha256"),
        ("initialize.sh", "allowed-database"),
        ("initialize.sh", "allowed-role"),
        ("initialize.sh", "allowed-ipv4-cidr"),
        ("initialize.sh", "allowed-ipv6-cidr"),
        ("start.sh", "listen-address"),
    ]
    with tempfile.TemporaryDirectory(prefix="amp-pg-template-") as temp:
        instance = Path(temp)
        base = instance / "postgresql"
        settings = base / "settings"
        settings.mkdir(parents=True)
        (base / "data").mkdir()
        (base / "run").mkdir()
        (base / "tls").mkdir()
        marker = instance / "injected"
        defaults = {
            "base-dir": base.as_posix() + "/",
            "port": "55432",
            "listen-address": "127.0.0.1",
            "release": "16",
            "version": "16.14",
            "source-sha256": "a" * 64,
            "allowed-database": "REPLACE_BEFORE_REMOTE_USE",
            "allowed-role": "REPLACE_BEFORE_REMOTE_USE",
            "allowed-ipv4-cidr": "127.0.0.1/32",
            "allowed-ipv6-cidr": "::1/128",
            "tls-hostname": "REPLACE_BEFORE_TLS_USE",
        }
        for script_name, setting in cases:
            for filename, value in defaults.items():
                (settings / filename).write_text(value, encoding="utf-8")
            (settings / setting).write_text(hostile, encoding="utf-8")
            environment = os.environ.copy()
            environment["HM_MARKER"] = marker.as_posix()
            result = subprocess.run(
                [bash, "-c", scripts[script_name]],
                cwd=instance,
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
