#!/usr/bin/env bash
set -euo pipefail

ExtractedScripts=${1:?Pass the extracted-script directory}
Base=/tmp/amp-postgresql-template/
TestUser=amp
Port=55432

for tool in bash bison flex gcc make openssl perl runuser sha256sum tar useradd wget; do
  command -v "$tool" >/dev/null || { echo "Missing required tool: $tool"; exit 1; }
done
test -f /usr/include/openssl/ssl.h

useradd --create-home --shell /bin/bash "$TestUser"
mkdir -p "${Base}data" "${Base}run" "${Base}tls" "${Base}releases"
chown -R "$TestUser:$TestUser" "$Base"

CommonEnv=(
  "HM_PG_BASE_DIR=$Base"
  "HM_PG_LISTEN_ADDRESS=127.0.0.1"
  "HM_PG_RELEASE=16"
  "HM_PG_VERSION=16.14"
  "HM_PG_SOURCE_SHA256=ca18d43510bbb09a271383e1aa705b05b76bc8e9400f9857178ba8ec54cf461a"
  "HM_PG_ALLOWED_DATABASE=REPLACE_BEFORE_REMOTE_USE"
  "HM_PG_ALLOWED_ROLE=REPLACE_BEFORE_REMOTE_USE"
  "HM_PG_ALLOWED_IPV4_CIDR=127.0.0.1/32"
  "HM_PG_ALLOWED_IPV6_CIDR=::1/128"
  "HM_PG_TLS_HOSTNAME=REPLACE_BEFORE_TLS_USE"
  "PGPORT=$Port"
)

run_stage() {
  local stage=$1
  runuser -u "$TestUser" -- env "${CommonEnv[@]}" bash "$ExtractedScripts/$stage"
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

HbaErrors=$(runuser -u "$TestUser" -- "${Base}pgsql/bin/psql" --host="${Base}run" --port="$Port" --username=amp --dbname=postgres --tuples-only --no-align --command="SELECT count(*) FROM pg_hba_file_rules WHERE error IS NOT NULL")
[[ "$HbaErrors" == '0' ]]
grep -Fxq 'hostnossl all all 0.0.0.0/0 reject' "${Base}data/pg_hba.conf"
grep -Fxq 'hostnossl all all ::0/0 reject' "${Base}data/pg_hba.conf"

runuser -u "$TestUser" -- env "PGDATA=${Base}data" "${Base}pgsql/bin/pg_ctl" -m fast stop
trap - EXIT
echo 'Pinned AMP image build, initialization, startup and readiness integration passed'
