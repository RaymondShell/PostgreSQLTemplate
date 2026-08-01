# PostgreSQL TLS template for CubeCoders AMP

Linux-only AMP Generic Module template for PostgreSQL built from official
source with OpenSSL support. It fixes the stock template's TLS-disabled source
build and defaults to no remotely usable database/role.

## Security properties

- AMP base image is referenced by OCI manifest digest, not a floating tag.
- PostgreSQL version and its official source SHA-256 are explicit settings.
- AMP renders update/start settings into separate data files; executable stages
  read and validate them instead of interpolating settings into shell source.
- The build requires `--with-ssl=openssl` and validates the exact installed
  `pg_config` and `postgres` binaries before switching the `pgsql` symlink.
- Existing data directories cannot be opened by another PostgreSQL major.
- Versioned installations are retained for binary rollback.
- Local database access uses operating-system peer authentication; no AMP
  database password is embedded in the template or process environment.
- Remote plaintext is rejected for IPv4 and IPv6 before narrow `hostssl` rules.
- Primary application, remote administration, commercial runtime, ticket
  authorizer and migration identities have separate bounded rules.
- Every remote CIDR is an exact IPv4 `/32` or IPv6 `/128`; HBA special tokens,
  the local `amp` superuser and duplicate remote identities are rejected.
- Remote application identities default to deliberately nonexistent roles plus
  loopback-only CIDRs.
- If no certificate is installed, local administration starts but all remote
  TCP access remains rejected.

## Add the repository to AMP

In the ADS instance, open:

`Configuration -> Instance Deployment -> Configuration Repositories`

Add and fetch:

```text
RaymondShell/PostgreSQLTemplate:main
```

Create a new instance using `PostgreSQL TLS`. Keep the exact PostgreSQL version
and SHA-256 paired. Never change major versions without `pg_upgrade` or a tested
logical dump/restore migration.

## Configure the bounded remote identities

The template stages the desired remote HBA atomically before every start, then
refreshes and directly invokes a self-locating renderer without a nested shell
command, then installs a local-only active HBA. After PostgreSQL is reachable
through its Unix socket, it verifies the configured role attributes, promotes
the staged rules and reloads them. A malformed setting or unsafe role stops
startup without ever activating remote access. AMP configuration changes
therefore take effect on the next ordinary restart; an AMP update is still
required once to install this template version.

After that verified PreStart gate, AMP launches a foreground supervisor. It
pins the exact postmaster PID, Linux process start identity, executable, data
directory, start epoch and port before emitting a fixed ready marker. AMP now
remains started for exactly as long as that postmaster identity remains valid.
On Stop, AMP sends `SIGTERM`; the supervisor revalidates the identity and asks
`pg_ctl` for a bounded fast shutdown. It never sends a signal to an unverified
raw postmaster PID.

The AMP console is therefore a read-only lifecycle log, not an interactive SQL
shell. Use pgAdmin for remote administration or open a local shell as the AMP
service identity and run `postgresql/pgsql/bin/psql` over the Unix socket.

Configure each purpose explicitly:

| Purpose | Database setting | Role setting | Source setting |
| --- | --- | --- | --- |
| BazaarManager | `bazaarmanager` | `bazaarmanager` | exact BazaarManager host IP |
| pgAdmin | `postgres,bazaarmanager,huntingmacro_commercial` | `pgadmin4_admin` | exact pgAdmin/VPN client IP |
| Commercial runtime | `huntingmacro_commercial` | `hm_commercial_runtime` | exact control-plane host IP |
| Ticket authorizer | `huntingmacro_commercial` | `hm_commercial_authorizer` | exact control-plane host IP |
| Migrations | `huntingmacro_commercial` | `hm_commercial_migrator` | exact deployment host IP |

For pgAdmin, enter the client PC's current public or private VPN address as an
exact `/32` in **Administrative IPv4 Host CIDR**. Leave the corresponding IPv6
setting at `::1/128` when IPv6 is not used. Prefer a private VPN address because
a public client address can change; update the AMP setting before restarting if
it does. Never commit a live client address to this repository.

The runtime, authorizer, migrator, administrator and primary application roles
must all be different. HBA only controls which connection tuple may reach
PostgreSQL; SQL grants still enforce what each login can do. Startup requires
every real remote role to be `NOSUPERUSER NOREPLICATION NOBYPASSRLS`. Primary,
runtime and authorizer must also be `LOGIN NOCREATEDB NOCREATEROLE`; runtime and
authorizer may not own the commercial database or have any role memberships.
The migrator must be `NOLOGIN NOCREATEDB NOCREATEROLE` at startup. The pgAdmin
role may retain `CREATEDB`/`CREATEROLE`, but it cannot be a superuser. Use a
local peer-authenticated `psql` shell for superuser-only work. Temporarily grant
the migrator
`LOGIN` only after a successful start, then restore `NOLOGIN` immediately after
the controlled migration and before the next restart.

Plain TCP is always rejected. Restrict the host firewall to the same exact
source addresses as a second layer.

## Install the certificate

Upload exactly these files through a protected administrative path:

```text
postgresql/tls/server.crt
postgresql/tls/server.key
```

`server.crt` must contain the hostname-valid leaf first followed by any
intermediates. `server.key` must be unencrypted, match it, be owned by the AMP
service identity, and have mode `0600`. Never store the CA signing key in AMP or
commit certificate/private-key material to this repository.

Restart the instance. PostgreSQL validates the certificate/key while starting;
the template also confirms `SHOW ssl` is `on` when a certificate is present.
Automate atomic certificate renewal/reload and expiry alerting before production.

## Acceptance checks

From a local peer-authenticated `psql` shell:

```sql
SHOW ssl;
SELECT ssl, version, cipher
FROM pg_catalog.pg_stat_ssl
WHERE pid = pg_backend_pid();

SELECT rule_number, type, database, user_name, address, auth_method, error
FROM pg_catalog.pg_hba_file_rules
ORDER BY rule_number;
```

From outside AMP, prove all of the following before migration or application
use:

- exact hostname and trusted CA succeed with `sslmode=verify-full`;
- wrong hostname and wrong CA fail;
- `sslmode=disable` is rejected;
- disallowed IPv4/IPv6 sources fail;
- only the configured purpose-specific database/role/source tuples match;
- all remote identities are distinct; and
- runtime and authorizer are non-owner, `NOSUPERUSER`, `NOREPLICATION` and
  `NOBYPASSRLS` while the migrator returns to `NOLOGIN` after deployment.

Rotate any credential previously placed in chat or logs. Do not include the TLS
private key in ordinary AMP exports. Use a clean shutdown/storage snapshot or
`pg_basebackup`, plus a tested encrypted logical dump, and perform a restore test
before replacing an existing database deployment.

## Updating

For a PostgreSQL security update within the same major:

1. obtain the exact release and `.tar.gz.sha256` value from the official
   PostgreSQL source directory;
2. update both AMP settings together;
3. take and restore-test a consistent backup;
4. stop/update/start the instance; and
5. repeat the TLS/HBA negative test matrix.

Template updates replace the lifecycle supervisor through a restricted staging
file. The staged file is set to mode `0500` before it is atomically renamed over
the active supervisor, so a failed write never leaves a partial executable at
the launch path and an existing read-only supervisor does not block the update.

Review and deliberately update the pinned AMP base-image digest when adopting a
new base image. Do not replace it with a floating tag.

The template offers supported PostgreSQL majors 15 through 18. Remove a major
from the AMP choices before its upstream end-of-life date; do not deploy a new
instance on a major that is approaching end of life.
