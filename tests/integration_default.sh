#!/usr/bin/env bash
set -euo pipefail

ExtractedScripts=${1:?Pass the extracted-script directory}
InstanceRoot=/tmp/amp-postgresql-template-instance
Base=${InstanceRoot}/postgresql/
Settings=${Base}settings
TestUser=amp
Port=55432

for tool in bash bison flex gcc make openssl perl runuser sha256sum tar useradd wget; do
  command -v "$tool" >/dev/null || { echo "Missing required tool: $tool"; exit 1; }
done
test -f /usr/include/openssl/ssl.h

useradd --create-home --shell /bin/bash "$TestUser"
mkdir -p "${Base}data" "${Base}run" "${Base}tls" "${Base}releases" "$Settings"
printf '%s' "$Base" > "$Settings/base-dir"
printf '%s' "$Port" > "$Settings/port"
printf '%s' '127.0.0.1' > "$Settings/listen-address"
printf '%s' '16' > "$Settings/release"
printf '%s' '16.14' > "$Settings/version"
printf '%s' 'ca18d43510bbb09a271383e1aa705b05b76bc8e9400f9857178ba8ec54cf461a' > "$Settings/source-sha256"
printf '%s' 'REPLACE_BEFORE_REMOTE_USE' > "$Settings/allowed-database"
printf '%s' 'REPLACE_BEFORE_REMOTE_USE' > "$Settings/allowed-role"
printf '%s' '127.0.0.1/32' > "$Settings/allowed-ipv4-cidr"
printf '%s' '::1/128' > "$Settings/allowed-ipv6-cidr"
printf '%s' 'REPLACE_BEFORE_TLS_USE' > "$Settings/tls-hostname"
chown -R "$TestUser:$TestUser" "$InstanceRoot"
cd "$InstanceRoot"

run_stage() {
  local stage=$1
  runuser -u "$TestUser" -- bash "$ExtractedScripts/$stage"
}

cleanup() {
  if [[ -x "${Base}pgsql/bin/pg_ctl" && -f "${Base}data/postmaster.pid" ]]; then
    runuser -u "$TestUser" -- env "PGDATA=${Base}data" "${Base}pgsql/bin/pg_ctl" -m immediate stop || true
  fi
}
trap cleanup EXIT

run_stage build.sh
"${Base}pgsql/bin/pg_config" --configure | grep -Fq -- '--with-ssl=openssl'
[[ $("${Base}pgsql/bin/postgres" -V) == 'postgres (PostgreSQL) 16.14' ]]
run_stage initialize.sh
run_stage start.sh
run_stage verify.sh

HbaDiagnostics=$(runuser -u "$TestUser" -- "${Base}pgsql/bin/psql" --host="${Base}run" --port="$Port" --username=amp --dbname=postgres --tuples-only --no-align --field-separator='|' --command="SELECT count(*) FROM pg_hba_file_rules WHERE error IS NOT NULL; SELECT count(*) FROM pg_hba_file_rules WHERE type = 'hostssl' AND error = 'hostssl record cannot match because SSL is disabled'")
[[ "$HbaDiagnostics" == $'2\n2' ]]
grep -Fxq 'hostnossl all all 0.0.0.0/0 reject' "${Base}data/pg_hba.conf"
grep -Fxq 'hostnossl all all ::0/0 reject' "${Base}data/pg_hba.conf"

runuser -u "$TestUser" -- env "PGDATA=${Base}data" "${Base}pgsql/bin/pg_ctl" -m fast stop
trap - EXIT
echo 'Pinned AMP image build, initialization, startup and readiness integration passed'
