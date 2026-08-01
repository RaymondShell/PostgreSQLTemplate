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
- Local AMP console access uses operating-system peer authentication; no AMP
  database password is embedded in the template or process environment.
- Remote plaintext is rejected for IPv4 and IPv6 before narrow `hostssl` rules.
- Remote access defaults to a deliberately nonexistent database and role plus
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

## Configure remote access

Before exposing the AMP port:

1. Set `Remote TLS Database` to the exact database.
2. Set `Remote TLS Role` to a dedicated `NOSUPERUSER NOBYPASSRLS` login, never
   the local `amp` superuser.
3. Set the IPv4/IPv6 CIDRs to the smallest control-plane or private VPN ranges.
4. Use the local AMP console to create the database/login with a new secret.
5. Keep the migration login disabled except during a controlled maintenance
   window; do not make it the template's permanent remote role.

The HBA file is regenerated deterministically on AMP updates. Plain TCP is
always rejected. Restrict the host firewall to the same CIDRs as an additional
layer.

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

From the local AMP console:

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
- only the configured database/role match remotely; and
- the runtime role is non-owner, `NOSUPERUSER` and `NOBYPASSRLS`.

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

Review and deliberately update the pinned AMP base-image digest when adopting a
new base image. Do not replace it with a floating tag.

The template offers supported PostgreSQL majors 15 through 18. Remove a major
from the AMP choices before its upstream end-of-life date; do not deploy a new
instance on a major that is approaching end of life.
