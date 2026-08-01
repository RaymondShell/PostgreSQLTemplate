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
printf '%s' 'postgres' > "$Settings/admin-databases"
printf '%s' 'REPLACE_ADMIN_BEFORE_REMOTE_USE' > "$Settings/admin-role"
printf '%s' '127.0.0.1/32' > "$Settings/admin-ipv4-cidr"
printf '%s' '::1/128' > "$Settings/admin-ipv6-cidr"
printf '%s' 'REPLACE_COMMERCIAL_DATABASE' > "$Settings/commercial-database"
printf '%s' 'REPLACE_COMMERCIAL_RUNTIME_ROLE' > "$Settings/commercial-runtime-role"
printf '%s' 'REPLACE_COMMERCIAL_AUTHORIZER_ROLE' > "$Settings/commercial-authorizer-role"
printf '%s' 'REPLACE_COMMERCIAL_MIGRATOR_ROLE' > "$Settings/commercial-migrator-role"
printf '%s' '127.0.0.1/32' > "$Settings/commercial-service-ipv4-cidr"
printf '%s' '::1/128' > "$Settings/commercial-service-ipv6-cidr"
printf '%s' '127.0.0.1/32' > "$Settings/commercial-migration-ipv4-cidr"
printf '%s' '::1/128' > "$Settings/commercial-migration-ipv6-cidr"
printf '%s' 'REPLACE_BEFORE_TLS_USE' > "$Settings/tls-hostname"
cp "$ExtractedScripts/render-hba.sh" "${Base}render-hba.sh"
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
run_stage regenerate-hba.sh
run_stage start.sh
run_stage verify.sh

Psql=("${Base}pgsql/bin/psql" --host="${Base}run" --port="$Port" --username=amp --dbname=postgres --set=ON_ERROR_STOP=1)
runuser -u "$TestUser" -- "${Psql[@]}" --command='CREATE ROLE "REPLACE_ADMIN_BEFORE_REMOTE_USE" LOGIN SUPERUSER'
runuser -u "$TestUser" -- env "PGDATA=${Base}data" "${Base}pgsql/bin/pg_ctl" -m fast stop
run_stage regenerate-hba.sh
run_stage start.sh
if run_stage verify.sh; then
  echo 'Existing placeholder superuser unexpectedly passed role-posture verification'
  exit 1
fi
! runuser -u "$TestUser" -- "${Base}pgsql/bin/pg_isready" --host="${Base}run" --port="$Port" >/dev/null 2>&1
[[ $(grep -c '^hostssl ' "${Base}data/pg_hba.conf" || true) == 0 ]]
run_stage start.sh
runuser -u "$TestUser" -- "${Psql[@]}" --command='DROP ROLE "REPLACE_ADMIN_BEFORE_REMOTE_USE"'
runuser -u "$TestUser" -- "${Psql[@]}" --command="CREATE ROLE bazaarmanager LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS"
runuser -u "$TestUser" -- "${Psql[@]}" --command="CREATE ROLE pgadmin4_admin LOGIN NOSUPERUSER CREATEDB CREATEROLE NOREPLICATION NOBYPASSRLS"
runuser -u "$TestUser" -- "${Psql[@]}" --command="CREATE ROLE hm_commercial_runtime LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS"
runuser -u "$TestUser" -- "${Psql[@]}" --command="CREATE ROLE hm_commercial_authorizer LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS"
runuser -u "$TestUser" -- "${Psql[@]}" --command="CREATE ROLE hm_commercial_migrator NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS"
runuser -u "$TestUser" -- "${Psql[@]}" --command="CREATE DATABASE bazaarmanager OWNER bazaarmanager"
runuser -u "$TestUser" -- "${Psql[@]}" --command="CREATE DATABASE huntingmacro_commercial OWNER amp"
runuser -u "$TestUser" -- env "PGDATA=${Base}data" "${Base}pgsql/bin/pg_ctl" -m fast stop

printf '%s' 'bazaarmanager' > "$Settings/allowed-database"
printf '%s' 'bazaarmanager' > "$Settings/allowed-role"
printf '%s' '192.0.2.10/32' > "$Settings/allowed-ipv4-cidr"
printf '%s' 'postgres,bazaarmanager,huntingmacro_commercial' > "$Settings/admin-databases"
printf '%s' 'pgadmin4_admin' > "$Settings/admin-role"
printf '%s' '203.0.113.10/32' > "$Settings/admin-ipv4-cidr"
printf '%s' 'huntingmacro_commercial' > "$Settings/commercial-database"
printf '%s' 'hm_commercial_runtime' > "$Settings/commercial-runtime-role"
printf '%s' 'hm_commercial_authorizer' > "$Settings/commercial-authorizer-role"
printf '%s' 'hm_commercial_migrator' > "$Settings/commercial-migrator-role"
printf '%s' '192.0.2.20/32' > "$Settings/commercial-service-ipv4-cidr"
printf '%s' '192.0.2.30/32' > "$Settings/commercial-migration-ipv4-cidr"
run_stage regenerate-hba.sh
[[ $(grep -c '^hostssl ' "${Base}data/pg_hba.conf" || true) == 0 ]]
run_stage start.sh
run_stage verify.sh

HbaDiagnostics=$(runuser -u "$TestUser" -- "${Base}pgsql/bin/psql" --host="${Base}run" --port="$Port" --username=amp --dbname=postgres --tuples-only --no-align --field-separator='|' --command="SELECT count(*) FROM pg_hba_file_rules WHERE error IS NOT NULL; SELECT count(*) FROM pg_hba_file_rules WHERE type = 'hostssl' AND error = 'hostssl record cannot match because SSL is disabled'")
[[ "$HbaDiagnostics" == $'10\n10' ]]
grep -Fxq 'hostnossl all all 0.0.0.0/0 reject' "${Base}data/pg_hba.conf"
grep -Fxq 'hostnossl all all ::0/0 reject' "${Base}data/pg_hba.conf"
[[ $(grep -c '^hostssl ' "${Base}data/pg_hba.conf") == 10 ]]
grep -Fxq 'hostssl postgres,bazaarmanager,huntingmacro_commercial pgadmin4_admin 203.0.113.10/32 scram-sha-256' "${Base}data/pg_hba.conf"
grep -Fxq 'hostssl huntingmacro_commercial hm_commercial_runtime 192.0.2.20/32 scram-sha-256' "${Base}data/pg_hba.conf"
grep -Fxq 'hostssl huntingmacro_commercial hm_commercial_migrator 192.0.2.30/32 scram-sha-256' "${Base}data/pg_hba.conf"

runuser -u "$TestUser" -- "${Psql[@]}" --command="CREATE ROLE elevated_group NOLOGIN SUPERUSER"
runuser -u "$TestUser" -- "${Psql[@]}" --command="CREATE ROLE elevated_bridge NOLOGIN"
runuser -u "$TestUser" -- "${Psql[@]}" --command="GRANT elevated_group TO elevated_bridge"
runuser -u "$TestUser" -- "${Psql[@]}" --command="GRANT elevated_bridge TO pgadmin4_admin"
runuser -u "$TestUser" -- env "PGDATA=${Base}data" "${Base}pgsql/bin/pg_ctl" -m fast stop
run_stage regenerate-hba.sh
run_stage start.sh
if run_stage verify.sh; then
  echo 'Recursive access to a non-AMP superuser unexpectedly passed verification'
  exit 1
fi
! runuser -u "$TestUser" -- "${Base}pgsql/bin/pg_isready" --host="${Base}run" --port="$Port" >/dev/null 2>&1
[[ $(grep -c '^hostssl ' "${Base}data/pg_hba.conf" || true) == 0 ]]
trap - EXIT
echo 'Pinned AMP image build, role-posture rejection and HBA promotion integration passed'
