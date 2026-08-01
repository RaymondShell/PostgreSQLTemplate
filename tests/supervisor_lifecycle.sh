#!/usr/bin/env bash
set -euo pipefail

SupervisorSource=${1:?Pass the extracted supervisor script}
TestRoot=$(mktemp -d)
declare -a TrackedPids=()

cleanup() {
  local Pid
  set +e
  for Pid in "${TrackedPids[@]}"; do
    kill -KILL "$Pid" >/dev/null 2>&1 || true
  done
  for Pid in "${TrackedPids[@]}"; do
    wait "$Pid" >/dev/null 2>&1 || true
  done
  rm -rf -- "$TestRoot"
}
trap cleanup EXIT

write_pidfile() {
  local Pid=$1
  printf '%s\n%s\n%s\n%s\n' "$Pid" "${Base}data" "$(date +%s)" "$Port" > "${Base}data/postmaster.pid"
}

setup_case() {
  local Name=$1
  CaseRoot="$TestRoot/$Name"
  Base="$CaseRoot/postgresql/"
  Port=55432
  mkdir -p "${Base}data" "${Base}run" "${Base}settings" "${Base}pgsql/bin"
  cp -- "$(command -v python3)" "${Base}pgsql/bin/postgres"
  cp -- "$SupervisorSource" "${Base}supervise.sh"
  chmod 0500 "${Base}supervise.sh" "${Base}pgsql/bin/postgres"
  printf '%s' "$Base" > "${Base}settings/base-dir"
  printf '%s' "$Port" > "${Base}settings/port"
  printf '%s\n' \
    '#!/usr/bin/env bash' \
    'set -euo pipefail' \
    'ScriptDirectory=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)' \
    'Base="${ScriptDirectory}/"' \
    'Port=$(<"${Base}settings/port")' \
    'printf "%s\n%s\n%s\n%s\n" "$$" "${Base}data" "$(date +%s)" "$Port" > "${Base}data/postmaster.pid"' \
    'printf "%s\n" launch >> "${Base}settings/startup-calls"' \
    'exec "${Base}pgsql/bin/postgres" -c '\''import signal,sys; signal.signal(signal.SIGINT, lambda *_: sys.exit(0)); signal.signal(signal.SIGTERM, lambda *_: sys.exit(0)); signal.pause()'\''' > "${Base}start-postgres.sh"
  printf '%s\n' \
    '#!/usr/bin/env bash' \
    'set -euo pipefail' \
    'ScriptDirectory=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)' \
    'printf "%s\n" verify >> "${ScriptDirectory}/settings/startup-calls"' > "${Base}verify-postgres.sh"
  printf '%s\n' '#!/usr/bin/env bash' 'exit 0' > "${Base}pgsql/bin/pg_isready"
  printf '%s\n' \
    '#!/usr/bin/env bash' \
    'set -euo pipefail' \
    'Bin=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)' \
    'Base=$(cd -- "$Bin/../.." && pwd -P)/' \
    'printf "%s\n" "$*" > "${Base}settings/pgctl-args"' \
    'Mode=$(<"${Base}settings/pgctl-mode")' \
    'if [[ "$Mode" == remove-pidfile ]]; then rm -f -- "${Base}data/postmaster.pid"; exit 1; fi' \
    '[[ "$Mode" == success ]] || exit 1' \
    'Pid=$(head -n1 -- "${Base}data/postmaster.pid")' \
    'kill -TERM "$Pid"' \
    'exit 0' > "${Base}pgsql/bin/pg_ctl"
  chmod 0500 "${Base}start-postgres.sh" "${Base}verify-postgres.sh" \
    "${Base}pgsql/bin/pg_isready" "${Base}pgsql/bin/pg_ctl"
  printf '%s' success > "${Base}settings/pgctl-mode"
}

start_supervisor() {
  SupervisorLog="$CaseRoot/supervisor.log"
  "${Base}supervise.sh" >"$SupervisorLog" 2>&1 &
  SupervisorPid=$!
  TrackedPids+=("$SupervisorPid")
  local Attempt
  for Attempt in {1..100}; do
    grep -Fxq 'AMP_POSTGRESQL_SUPERVISOR_READY' "$SupervisorLog" 2>/dev/null && break
    kill -0 "$SupervisorPid" 2>/dev/null || {
      echo "Supervisor exited before readiness: $(<"$SupervisorLog")"
      return 1
    }
    sleep 0.02
  done
  grep -Fxq 'AMP_POSTGRESQL_SUPERVISOR_READY' "$SupervisorLog" 2>/dev/null || {
    echo 'Supervisor did not emit its fixed readiness marker'
    return 1
  }
  PostgresPid=$(head -n1 -- "${Base}data/postmaster.pid")
  [[ "$PostgresPid" =~ ^[0-9]+$ ]]
  TrackedPids+=("$PostgresPid")
  [[ $(<"${Base}settings/startup-calls") == $'launch\nverify' ]]
}

setup_case normal_stop
start_supervisor
kill -TERM "$SupervisorPid"
wait "$SupervisorPid"
wait "$PostgresPid" >/dev/null 2>&1 || true
if kill -0 "$PostgresPid" 2>/dev/null; then
  echo 'Normal supervisor stop left the managed postmaster alive'
  exit 1
fi
grep -Fxq 'Verified PostgreSQL postmaster stopped cleanly' "$SupervisorLog"
grep -Fq -- '--mode=fast --wait --timeout=25' "${Base}settings/pgctl-args"

setup_case stop_during_verification
chmod 0700 "${Base}verify-postgres.sh"
printf '%s\n' \
  '#!/usr/bin/env bash' \
  'set -euo pipefail' \
  'Base=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)/' \
  'touch "${Base}settings/verify-entered"' \
  "trap 'exit 143' TERM INT HUP" \
  'while true; do sleep 1; done' > "${Base}verify-postgres.sh"
chmod 0500 "${Base}verify-postgres.sh"
SupervisorLog="$CaseRoot/supervisor.log"
"${Base}supervise.sh" >"$SupervisorLog" 2>&1 &
SupervisorPid=$!
TrackedPids+=("$SupervisorPid")
for Attempt in {1..100}; do
  [[ -f "${Base}settings/verify-entered" ]] && break
  kill -0 "$SupervisorPid" 2>/dev/null || {
    echo 'Supervisor exited before entering verification'
    exit 1
  }
  sleep 0.02
done
[[ -f "${Base}settings/verify-entered" ]] || {
  echo "Supervisor never entered verification: $(<"$SupervisorLog")"
  exit 1
}
PostgresPid=$(head -n1 -- "${Base}data/postmaster.pid")
TrackedPids+=("$PostgresPid")
kill -TERM "$SupervisorPid"
wait "$SupervisorPid"
wait "$PostgresPid" >/dev/null 2>&1 || true
if kill -0 "$PostgresPid" 2>/dev/null; then
  echo 'Stop during verification left the managed postmaster alive'
  exit 1
fi
! grep -Fq 'AMP_POSTGRESQL_SUPERVISOR_READY' "$SupervisorLog"
grep -Fxq 'Verified PostgreSQL postmaster stopped cleanly' "$SupervisorLog"

setup_case failed_verification
chmod 0700 "${Base}verify-postgres.sh"
printf '%s\n' \
  '#!/usr/bin/env bash' \
  'set -euo pipefail' \
  'Base=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)/' \
  'touch "${Base}settings/verify-entered"' \
  'sleep 0.2' \
  'exit 1' > "${Base}verify-postgres.sh"
chmod 0500 "${Base}verify-postgres.sh"
SupervisorLog="$CaseRoot/supervisor.log"
"${Base}supervise.sh" >"$SupervisorLog" 2>&1 &
SupervisorPid=$!
TrackedPids+=("$SupervisorPid")
for Attempt in {1..100}; do
  [[ -f "${Base}settings/verify-entered" ]] && break
  kill -0 "$SupervisorPid" 2>/dev/null || break
  sleep 0.01
done
PostgresPid=$(head -n1 -- "${Base}data/postmaster.pid")
TrackedPids+=("$PostgresPid")
if wait "$SupervisorPid"; then
  echo 'Supervisor reported success after verification failed'
  exit 1
fi
wait "$PostgresPid" >/dev/null 2>&1 || true
if kill -0 "$PostgresPid" 2>/dev/null; then
  echo 'Failed verification left the managed postmaster alive'
  exit 1
fi
grep -Fq 'PostgreSQL verification failed with status 1' "$SupervisorLog"
grep -Fxq 'Verified PostgreSQL postmaster stopped cleanly' "$SupervisorLog"

setup_case unexpected_exit
start_supervisor
kill -KILL "$PostgresPid"
wait "$PostgresPid" >/dev/null 2>&1 || true
if wait "$SupervisorPid"; then
  echo 'Supervisor accepted an unexpected postmaster exit'
  exit 1
fi
grep -Fq 'exited or lost its identity unexpectedly' "$SupervisorLog"

setup_case replaced_pidfile
start_supervisor
"${Base}pgsql/bin/postgres" -c 'import signal,sys; signal.signal(signal.SIGINT, lambda *_: sys.exit(0)); signal.signal(signal.SIGTERM, lambda *_: sys.exit(0)); signal.pause()' &
ReplacementPid=$!
TrackedPids+=("$ReplacementPid")
write_pidfile "$ReplacementPid"
if wait "$SupervisorPid"; then
  echo 'Supervisor accepted a replaced postmaster PID file'
  exit 1
fi
wait "$PostgresPid" >/dev/null 2>&1 || true
! kill -0 "$PostgresPid" 2>/dev/null
kill -0 "$ReplacementPid"
grep -Fq 'identity changed unexpectedly' "$SupervisorLog"
grep -Fq 'Refusing pg_ctl shutdown because the PostgreSQL PID file identity changed' "$SupervisorLog"

setup_case failed_pgctl
printf '%s' failure > "${Base}settings/pgctl-mode"
start_supervisor
kill -TERM "$SupervisorPid"
wait "$SupervisorPid"
wait "$PostgresPid" >/dev/null 2>&1 || true
! kill -0 "$PostgresPid" 2>/dev/null
grep -Fq 'pg_ctl shutdown was unavailable; signaling the verified direct PostgreSQL child' "$SupervisorLog"
grep -Fq -- '--mode=fast --wait --timeout=25' "${Base}settings/pgctl-args"

setup_case lost_pidfile_during_failed_stop
printf '%s' remove-pidfile > "${Base}settings/pgctl-mode"
start_supervisor
kill -TERM "$SupervisorPid"
wait "$SupervisorPid"
wait "$PostgresPid" >/dev/null 2>&1 || true
! kill -0 "$PostgresPid" 2>/dev/null
grep -Fq 'pg_ctl shutdown was unavailable; signaling the verified direct PostgreSQL child' "$SupervisorLog"

echo 'Supervisor readiness, signal, identity-loss, PID-replacement and bounded-stop tests passed'
