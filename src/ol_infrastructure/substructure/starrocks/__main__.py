"""Configure Vault dynamic database credentials and StarRocks cluster state.

StarRocks exposes a MySQL-compatible protocol on port 9030 (the FE query port).
The built-in Vault MySQL plugin cannot be used because StarRocks only supports
COM_STMT_PREPARE for SELECT statements, returning error 1295 for all DDL and
privilege-management statements (CREATE USER, GRANT, DROP USER). Vault's MySQL
plugin uses COM_STMT_PREPARE for all creation/revocation statements, causing a
nil-pointer panic in the plugin.

Instead, we use a custom database plugin (vault-plugin-database-starrocks) that
sends all statements via COM_QUERY (db.ExecContext with no bind parameters),
which StarRocks supports for all statement types.

The plugin binary must be present at /var/lib/vault/plugins/ on the Vault server
before this stack is applied.  The Vault AMI build downloads it automatically.

Privilege model: native StarRocks roles (readonly / app / admin) own the full
privilege definitions and are created by the roles-setup Command resource below.
Vault-issued dynamic users are assigned the matching native role at creation time
via GRANT <role> TO USER, keeping the Vault create statements minimal.

Connectivity: Vault must be able to reach the StarRocks FE MySQL port (9030)
over the VPC. Configure starrocks:fe_host to an internally routable address
(e.g. an internal NLB DNS name or a stable VPC-accessible hostname).
The SQL setup Command resources require the mysql client on the Pulumi runner
(Concourse worker) and network access to the same NLB endpoint.
"""

import hashlib
import json
import re
from pathlib import Path

import pulumi
import pulumi_command as command
import pulumi_vault as vault
from pulumi import Config, InvokeOptions, ResourceOptions, export
from pulumi_vault.generic.get_secret import get_secret_output as vault_get_secret_output

from bridge.lib.magic_numbers import ONE_MONTH_SECONDS
from bridge.lib.versions import VAULT_PLUGIN_STARROCKS_SHA256
from ol_infrastructure.lib import pulumi_projects
from ol_infrastructure.lib.pulumi_helper import (
    make_stack_reference,
    parse_stack,
    require_stack_output_value,
)
from ol_infrastructure.lib.vault import setup_vault_provider

_vault_provider = setup_vault_provider()
stack_info = parse_stack()
starrocks_config = Config("starrocks")

env_name = f"data-{stack_info.env_suffix}"
# Each environment (dev/qa/ci/production) runs its own, entirely separate Vault
# deployment (vault-qa.odl.mit.edu, vault-production.odl.mit.edu, ...), so the
# mount path itself doesn't need an env suffix -- the Vault server it lives in
# already provides that scoping.
mount_point = "database-starrocks"

cluster_stack = make_stack_reference(pulumi_projects.EKS, f"data.{stack_info.name}")
_kube_config_raw = require_stack_output_value(cluster_stack, "kube_config")
_kube_config: str = (
    json.dumps(_kube_config_raw)
    if isinstance(_kube_config_raw, dict)
    else _kube_config_raw
)

STARROCKS_MYSQL_PORT = 9030
MAX_TTL = ONE_MONTH_SECONDS * 6
DEFAULT_TTL = ONE_MONTH_SECONDS * 3

# Read the FE MySQL NLB hostname and admin password from the applications stack so
# both values stay in sync with the deployed cluster without duplication.
applications_stack = make_stack_reference(
    pulumi_projects.STARROCKS_APP, starrocks_config.require("applications_stack_name")
)
db_host = applications_stack.require_output("fe_mysql_host")
db_admin_password = applications_stack.require_output("root_password_secret")

db_port = starrocks_config.get_int("fe_mysql_port") or STARROCKS_MYSQL_PORT
db_admin_username = starrocks_config.require("db_admin_username")

# StarRocks-compatible SQL role statements for Vault dynamic credentials.
# Each list entry is executed as a separate statement in the same connection.
#
# Privilege definitions live on the native StarRocks roles (readonly / app /
# admin), which are created and maintained by the roles-setup Command resource
# below.  Vault-issued users are created with a password and then assigned the
# matching native role; dropping the user at revocation time is sufficient.
starrocks_role_statements: dict[str, dict[str, list[str]]] = {
    "readonly": {
        "create": [
            "CREATE USER '{{name}}'@'%' IDENTIFIED BY '{{password}}';",
            "GRANT readonly TO USER '{{name}}'@'%';",
            "ALTER USER '{{name}}'@'%' DEFAULT ROLE readonly;",
        ],
        "revoke": ["DROP USER '{{name}}'@'%';"],
        "renew": [],
        "rollback": [],
    },
    "app": {
        "create": [
            "CREATE USER '{{name}}'@'%' IDENTIFIED BY '{{password}}';",
            "GRANT app TO USER '{{name}}'@'%';",
            "ALTER USER '{{name}}'@'%' DEFAULT ROLE app;",
        ],
        "revoke": ["DROP USER '{{name}}'@'%';"],
        "renew": [],
        "rollback": [],
    },
    "admin": {
        "create": [
            "CREATE USER '{{name}}'@'%' IDENTIFIED BY '{{password}}';",
            "GRANT admin TO USER '{{name}}'@'%';",
            "ALTER USER '{{name}}'@'%' DEFAULT ROLE admin;",
        ],
        "revoke": ["DROP USER '{{name}}'@'%';"],
        "renew": [],
        "rollback": [],
    },
}

# Connection URL uses Go MySQL DSN format required by Vault's MySQL plugin.
# Vault substitutes {{username}} and {{password}} with the admin credentials
# at connection time (distinct from {{name}}/{{password}} in role statements
# which are the dynamically-generated credential placeholders).
# db_host is an Output[str] from the applications stack reference, so we
# resolve it via .apply() rather than an f-string.
#
# tls=skip-verify encrypts the connection but skips server certificate name
# verification because Vault connects via the NLB's AWS hostname, which does
# not match the cert's SAN (lakehouse[.qa].starrocks.ol.mit.edu).
ssl_enabled = starrocks_config.get_bool("ssl_enabled") or False
_tls_param = "?tls=skip-verify" if ssl_enabled else ""
connection_url = db_host.apply(
    lambda host: f"{{{{username}}}}:{{{{password}}}}@tcp({host}:{db_port})/{_tls_param}"
)

starrocks_vault_mount = vault.Mount(
    f"starrocks-{stack_info.env_suffix}-vault-database-mount",
    path=mount_point,
    type="database",
    max_lease_ttl_seconds=MAX_TTL,
    default_lease_ttl_seconds=DEFAULT_TTL,
    description=f"Dynamic credentials for StarRocks ({env_name})",
    opts=ResourceOptions(delete_before_replace=True),
)

# Register the custom StarRocks database plugin in Vault's plugin catalog.
# The binary must already be present at /var/lib/vault/plugins/ on the Vault
# server (placed there by the Vault AMI build).
starrocks_plugin = vault.Plugin(
    f"starrocks-{stack_info.env_suffix}-vault-database-plugin",
    type="database",
    name="starrocks-database-plugin",
    command="vault-plugin-database-starrocks",
    sha256=VAULT_PLUGIN_STARROCKS_SHA256,
    opts=ResourceOptions(delete_before_replace=True),
)

# Configure the StarRocks connection using generic.Secret because the Pulumi
# vault provider's SecretBackendConnection resource only supports built-in
# plugin names (mysql, mysql_rds, etc.) via dedicated blocks. Our custom
# plugin is configured by writing directly to the Vault API path.
#
# disable_read=True is required because Vault redacts the password field in GET
# responses, which would cause a perpetual diff on every Pulumi plan.
starrocks_db_connection = vault.generic.Secret(
    f"starrocks-{stack_info.env_suffix}-vault-database-connection",
    path=pulumi.Output.concat(starrocks_vault_mount.path, "/config/starrocks"),
    disable_read=True,
    data_json=pulumi.Output.all(connection_url, db_admin_password).apply(
        lambda args: json.dumps(
            {
                "plugin_name": "starrocks-database-plugin",
                "connection_url": args[0],
                "username": db_admin_username,
                "password": args[1],
                "allowed_roles": sorted(starrocks_role_statements.keys()),
                "verify_connection": (
                    starrocks_config.get_bool("verify_connection") or False
                ),
                "username_template": (
                    '{{printf "v-%.8s-%.8s-%.20s"'
                    " (.DisplayName) (.RoleName) (random 20) | truncate 32}}"
                ),
            }
        )
    ),
    opts=ResourceOptions(
        parent=starrocks_vault_mount,
        delete_before_replace=True,
        depends_on=[starrocks_vault_mount, starrocks_plugin],
    ),
)

for role_name, role_defs in starrocks_role_statements.items():
    vault.database.SecretBackendRole(
        f"starrocks-{stack_info.env_suffix}-vault-database-role-{role_name}",
        backend=starrocks_vault_mount.path,
        db_name="starrocks",
        name=role_name,
        creation_statements=role_defs["create"],
        revocation_statements=role_defs["revoke"],
        renew_statements=role_defs["renew"],
        rollback_statements=role_defs["rollback"],
        max_ttl=MAX_TTL,
        default_ttl=DEFAULT_TTL,
        opts=ResourceOptions(
            parent=starrocks_db_connection,
            delete_before_replace=True,
            depends_on=[starrocks_db_connection],
        ),
    )

# ============================================================================
# StarRocks SQL setup via local.Command
#
# Each Command resource pipes SQL through the mysql CLI using environment
# variables for credentials (MYSQL_PWD / STARROCKS_HOST) so that secrets
# never appear in the command string, process list, or Pulumi state.
#
# The same SQL is re-applied on both create and update, which is safe because:
#   - CREATE ... IF NOT EXISTS and CREATE ROLE IF NOT EXISTS are no-ops when
#     the object already exists.
#   - GRANT is idempotent in StarRocks (re-granting an existing privilege is
#     silently ignored).
#   - ALTER CATALOG SET patches only the listed properties in-place.
#
# Triggers are a SHA-256 hash of the SQL string; any config change
# (e.g. aws_region) causes the update command to re-run automatically.
# ============================================================================

# MariaDB client (used in the Pulumi runner image) quirks:
#   1. Does not support --ssl-mode=REQUIRED (MySQL 5.7.11+ syntax).
#   2. Does not read MYSQL_PWD from the environment.
#   3. With a password present and --ssl, it verifies the server certificate
#      by default, which fails against internal-CA certs.
#
# Solution: write a --defaults-extra-file at runtime containing the password
# and ssl-verify-server-cert=OFF.  The file is written from $MYSQL_PWD (a
# shell variable expanded from the Pulumi secrets env dict) so the secret
# value is never stored in Pulumi state.  The temp file is removed after the
# mysql invocation regardless of exit status.
_ssl_flag = " --ssl" if ssl_enabled else " --skip-ssl"
_mysql_opts_setup = (
    "_pwfile=$(mktemp)"
    " && printf '[client]\\npassword=%s\\nssl-verify-server-cert=OFF\\n'"
    ' "$MYSQL_PWD" > "$_pwfile"'
)
_mysql_opts_cleanup = '_rc=$?; rm -f "$_pwfile"; exit $_rc'
_mysql_client = (
    f'mysql --defaults-extra-file="$_pwfile" -h"$STARROCKS_HOST" -P{db_port}'
    f" -u{db_admin_username}{_ssl_flag}"
)
# Two stdin-pipe helpers so that multi-statement SQL is always sent through
# COM_QUERY without --force: _exec_sql runs $STARROCKS_SQL (create/update),
# _exec_delete_sql runs $STARROCKS_DELETE_SQL (delete).  Using separate env
# vars lets each Command resource carry both payloads in its environment dict.
_exec_sql = (
    f"{_mysql_opts_setup}"
    f" && printf '%s' \"$STARROCKS_SQL\" | {_mysql_client}"
    f"; {_mysql_opts_cleanup}"
)
_exec_delete_sql = (
    f"{_mysql_opts_setup}"
    f" && printf '%s' \"$STARROCKS_DELETE_SQL\" | {_mysql_client}"
    f"; {_mysql_opts_cleanup}"
)

# Environment shared by all Command resources: credentials never appear in the
# create/update/delete strings (they are masked as Pulumi secrets in the env dict).
_mysql_env: dict[str, pulumi.Input[str]] = {
    "MYSQL_PWD": db_admin_password,
    "STARROCKS_HOST": db_host,
}

enable_data_lake = starrocks_config.get_bool("enable_data_lake_integration") or False
oidc_enabled = starrocks_config.get_bool("oidc_enabled") or False

# --- Iceberg catalogs -------------------------------------------------------
# Both QA and Production catalogs are registered in every StarRocks instance.
# Production datasets are more complete, so having them available in QA
# simplifies testing against Superset without requiring a separate environment.
#
# CREATE IF NOT EXISTS is idempotent: it is a no-op when the catalog
# already exists with any set of properties.  StarRocks has no ALTER CATALOG
# statement for updating properties in-place; the only way to change catalog
# properties after creation is to DROP and re-CREATE the catalog.  If
# properties need to change, manually run
#   DROP CATALOG IF EXISTS <name>;
# against the FE, then run `pulumi up` to recreate it via this command.
#
# Credential chain: use_instance_profile=false with no explicit key/role
# instructs StarRocks to fall through to the AWS SDK default credential
# chain.  On EKS with IRSA the pod already has AWS_ROLE_ARN +
# AWS_WEB_IDENTITY_TOKEN_FILE injected; the SDK resolves them automatically
# without a second sts:AssumeRole call.  Setting iam_role_arn here would
# cause StarRocks to attempt a nested AssumeRole and fail with a 403.
_DATA_LAKE_ENVS = ["qa", "production"]
catalog_setups: list[command.local.Command] = []
_iceberg_roles_sql = ""
if enable_data_lake:
    aws_region = starrocks_config.get("aws_region") or "us-east-1"
    _catalog_conn_props = (
        f'    "aws.glue.use_instance_profile" = "false",\n'
        f'    "aws.glue.region" = "{aws_region}",\n'
        f'    "aws.s3.use_instance_profile" = "false",\n'
        f'    "aws.s3.region" = "{aws_region}"'
    )

    for _catalog_env in _DATA_LAKE_ENVS:
        _catalog_name = f"ol_data_lake_{_catalog_env}"
        _catalog_sql = (
            f"CREATE EXTERNAL CATALOG IF NOT EXISTS {_catalog_name}\n"
            f"COMMENT 'MIT OL Data Lake {_catalog_env.capitalize()} Iceberg Catalog"
            f" (AWS Glue)'\n"
            f"PROPERTIES(\n"
            f'    "type" = "iceberg",\n'
            f'    "iceberg.catalog.type" = "glue",\n'
            f"{_catalog_conn_props}\n"
            f");"
        )
        catalog_setups.append(
            command.local.Command(
                f"starrocks-{stack_info.env_suffix}-catalog-setup-{_catalog_env}",
                create=_exec_sql,
                update=_exec_sql,
                delete=_exec_delete_sql,
                environment={
                    **_mysql_env,
                    "STARROCKS_SQL": _catalog_sql,
                    "STARROCKS_DELETE_SQL": f"DROP CATALOG IF EXISTS {_catalog_name};",
                },
                triggers=[hashlib.sha256(_catalog_sql.encode()).hexdigest()],
                opts=ResourceOptions(
                    delete_before_replace=True,
                    depends_on=[starrocks_db_connection],
                ),
            )
        )
        _iceberg_roles_sql += (
            f"\nGRANT USAGE ON CATALOG {_catalog_name} TO ROLE readonly;"
            f"\nSET CATALOG {_catalog_name};"
            f"\nGRANT SELECT ON ALL TABLES IN ALL DATABASES TO ROLE readonly;"
            f"\nSET CATALOG default_catalog;"
            f"\n"
            f"\nGRANT USAGE ON CATALOG {_catalog_name} TO ROLE app;"
            f"\nSET CATALOG {_catalog_name};"
            "\nGRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN ALL DATABASES"
            " TO ROLE app;"
            f"\nSET CATALOG default_catalog;"
            f"\n"
            f"\nGRANT USAGE ON CATALOG {_catalog_name} TO ROLE admin;"
            f"\nSET CATALOG {_catalog_name};"
            f"\nGRANT ALL ON ALL TABLES IN ALL DATABASES TO ROLE admin;"
            f"\nSET CATALOG default_catalog;"
            # Governance roles: all get USAGE on the catalog; full vs. read-only
            # access is differentiated below.  Database-level scoping within
            # the catalog (bronze / silver_* / gold_*) should be added once
            # the Glue catalog database naming is confirmed.
            f"\n"
            f"\nGRANT USAGE ON CATALOG {_catalog_name} TO ROLE ol_platform_admin;"
            f"\nSET CATALOG {_catalog_name};"
            f"\nGRANT ALL ON ALL TABLES IN ALL DATABASES TO ROLE ol_platform_admin;"
            f"\nGRANT ALL ON ALL DATABASES TO ROLE ol_platform_admin;"
            f"\nSET CATALOG default_catalog;"
            f"\n"
            f"\nGRANT USAGE ON CATALOG {_catalog_name} TO ROLE ol_data_engineer;"
            f"\nSET CATALOG {_catalog_name};"
            f"\nGRANT ALL ON ALL TABLES IN ALL DATABASES TO ROLE ol_data_engineer;"
            f"\nGRANT ALL ON ALL DATABASES TO ROLE ol_data_engineer;"
            f"\nSET CATALOG default_catalog;"
            f"\n"
            f"\nGRANT USAGE ON CATALOG {_catalog_name} TO ROLE ol_data_analyst;"
            f"\nSET CATALOG {_catalog_name};"
            f"\nGRANT SELECT ON ALL TABLES IN ALL DATABASES TO ROLE ol_data_analyst;"
            f"\nSET CATALOG default_catalog;"
            f"\n"
            f"\nGRANT USAGE ON CATALOG {_catalog_name} TO ROLE ol_researcher;"
            f"\nSET CATALOG {_catalog_name};"
            f"\nGRANT SELECT ON ALL TABLES IN ALL DATABASES TO ROLE ol_researcher;"
            f"\nSET CATALOG default_catalog;"
            f"\n"
            f"\nGRANT USAGE ON CATALOG {_catalog_name} TO ROLE ol_instructor;"
            f"\nSET CATALOG {_catalog_name};"
            f"\nGRANT SELECT ON ALL TABLES IN ALL DATABASES TO ROLE ol_instructor;"
            f"\nSET CATALOG default_catalog;"
            f"\n"
            f"\nGRANT USAGE ON CATALOG {_catalog_name} TO ROLE ol_business_analyst;"
            f"\nSET CATALOG {_catalog_name};"
            "\nGRANT SELECT ON ALL TABLES IN ALL DATABASES"
            " TO ROLE ol_business_analyst;"
            f"\nSET CATALOG default_catalog;"
        )

# --- Native StarRocks roles -------------------------------------------------
# Permanent roles for OIDC-authenticated users and direct database access.
# These parallel the Vault dynamic credential roles (readonly / app / admin)
# but are database-level roles rather than per-session Vault-issued users.
#
# GRANT is idempotent in StarRocks: re-granting an existing privilege is a
# no-op, so this block is safe to re-apply on every `pulumi up`.
#
# Iceberg catalog grants are appended when enable_data_lake_integration is
# true.  SET CATALOG switches the session context so the subsequent table-level
# GRANT applies to the Iceberg catalog's databases rather than default_catalog.
#
# LIMITATION: these GRANTs are additive.  If enable_data_lake_integration is
# later set to false the Iceberg catalog grants already on the roles are NOT
# automatically revoked.  A manual REVOKE USAGE ON CATALOG + REVOKE on tables
# is required to remove them (or DROP + recreate the roles via pulumi destroy
# followed by pulumi up).
_base_roles_sql = """\
-- Machine-access roles assigned to Vault-issued ephemeral service accounts.
-- These are referenced by the vault.database.SecretBackendRole resources above.
CREATE ROLE IF NOT EXISTS readonly;
CREATE ROLE IF NOT EXISTS app;
CREATE ROLE IF NOT EXISTS admin;

-- Governance roles for OIDC-authenticated human users.  Names match the
-- Keycloak ol-starrocks-client roles exactly for 1:1 claim-to-role mapping.
-- Iceberg catalog grants are appended per-catalog when
-- enable_data_lake_integration is true.  Database-level scoping within the
-- Iceberg catalog (e.g. restricting ol_instructor to gold_analytics /
-- gold_operations) should be layered on top once the Glue catalog database
-- naming is confirmed.
CREATE ROLE IF NOT EXISTS ol_platform_admin;
CREATE ROLE IF NOT EXISTS ol_data_engineer;
CREATE ROLE IF NOT EXISTS ol_data_analyst;
CREATE ROLE IF NOT EXISTS ol_researcher;
CREATE ROLE IF NOT EXISTS ol_instructor;
CREATE ROLE IF NOT EXISTS ol_business_analyst;

GRANT USAGE ON CATALOG default_catalog TO ROLE readonly;
GRANT SELECT ON ALL TABLES IN ALL DATABASES TO ROLE readonly;

GRANT USAGE ON CATALOG default_catalog TO ROLE app;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN ALL DATABASES TO ROLE app;

GRANT cluster_admin TO ROLE admin;
GRANT db_admin TO ROLE admin;
GRANT user_admin TO ROLE admin;
GRANT USAGE ON CATALOG default_catalog TO ROLE admin;
GRANT ALL ON ALL TABLES IN ALL DATABASES TO ROLE admin;
GRANT ALL ON ALL DATABASES TO ROLE admin;

-- ol_platform_admin: full cluster management + governance oversight
GRANT cluster_admin TO ROLE ol_platform_admin;
GRANT db_admin TO ROLE ol_platform_admin;
GRANT user_admin TO ROLE ol_platform_admin;
GRANT USAGE ON CATALOG default_catalog TO ROLE ol_platform_admin;
GRANT ALL ON ALL TABLES IN ALL DATABASES TO ROLE ol_platform_admin;
GRANT ALL ON ALL DATABASES TO ROLE ol_platform_admin;

-- ol_data_engineer: pipeline development; full data access, no cluster admin
GRANT db_admin TO ROLE ol_data_engineer;
GRANT USAGE ON CATALOG default_catalog TO ROLE ol_data_engineer;
GRANT ALL ON ALL TABLES IN ALL DATABASES TO ROLE ol_data_engineer;
GRANT ALL ON ALL DATABASES TO ROLE ol_data_engineer;

-- The four read-only roles below are currently granted catalog-wide SELECT
-- (ALL TABLES IN ALL DATABASES).  The per-database governance scoping noted in
-- each "target scope" comment (e.g. restricting analysts to Silver_Analytics +
-- Gold) is NOT yet enforced here; it is deferred until the Glue/Iceberg
-- database naming is confirmed, at which point these grants will be narrowed to
-- the named databases.  Until then all four roles see the same read surface.
--
-- ol_data_analyst: target scope Silver_Analytics + Gold (read-only)
GRANT USAGE ON CATALOG default_catalog TO ROLE ol_data_analyst;
GRANT SELECT ON ALL TABLES IN ALL DATABASES TO ROLE ol_data_analyst;

-- ol_researcher: target scope Silver_Analytics + Gold_Analytics (read-only)
GRANT USAGE ON CATALOG default_catalog TO ROLE ol_researcher;
GRANT SELECT ON ALL TABLES IN ALL DATABASES TO ROLE ol_researcher;

-- ol_instructor: target scope Gold_Analytics + Gold_Operations (read-only)
GRANT USAGE ON CATALOG default_catalog TO ROLE ol_instructor;
GRANT SELECT ON ALL TABLES IN ALL DATABASES TO ROLE ol_instructor;

-- ol_business_analyst: target scope Silver_Operations + Gold (read-only)
GRANT USAGE ON CATALOG default_catalog TO ROLE ol_business_analyst;
GRANT SELECT ON ALL TABLES IN ALL DATABASES TO ROLE ol_business_analyst;"""

_roles_sql = _base_roles_sql + _iceberg_roles_sql
_role_deps: list[pulumi.Resource] = [starrocks_db_connection, *catalog_setups]

_roles_drop_sql = (
    "DROP ROLE IF EXISTS readonly; DROP ROLE IF EXISTS app; DROP ROLE IF EXISTS admin;"
    " DROP ROLE IF EXISTS ol_platform_admin; DROP ROLE IF EXISTS ol_data_engineer;"
    " DROP ROLE IF EXISTS ol_data_analyst; DROP ROLE IF EXISTS ol_researcher;"
    " DROP ROLE IF EXISTS ol_instructor; DROP ROLE IF EXISTS ol_business_analyst;"
)

# Valid Keycloak ol-starrocks-client role names.  These match StarRocks role
# names 1:1 so no translation is needed when creating OIDC user accounts.
_GOVERNANCE_ROLES: frozenset[str] = frozenset(
    {
        "ol_platform_admin",
        "ol_data_engineer",
        "ol_data_analyst",
        "ol_researcher",
        "ol_instructor",
        "ol_business_analyst",
    }
)

# Allowlist for OIDC usernames before they are interpolated into SQL string
# literals.  Keycloak preferred_username values are emails or short login ids;
# restricting to this character set prevents SQL-breaking / injection input
# (quotes, backslashes, whitespace, control characters) from reaching the
# generated CREATE USER / GRANT statements.
_OIDC_USERNAME_RE = re.compile(r"^[A-Za-z0-9._%+@-]+$")

roles_setup_cmd = command.local.Command(
    f"starrocks-{stack_info.env_suffix}-roles-setup",
    create=_exec_sql,
    update=_exec_sql,
    delete=_exec_delete_sql,
    environment={
        **_mysql_env,
        "STARROCKS_SQL": _roles_sql,
        "STARROCKS_DELETE_SQL": _roles_drop_sql,
    },
    triggers=[hashlib.sha256(_roles_sql.encode()).hexdigest()],
    opts=ResourceOptions(delete_before_replace=True, depends_on=_role_deps),
)

# --- b2b_analytics database ---------------------------------------------
# Dedicated database (default_catalog) backing the B2B self-serve analytics
# StarRocks MVs (see hq#10006 / hq#10012). The `app` role already has
# SELECT/INSERT/UPDATE/DELETE ON ALL TABLES IN ALL DATABASES from the base
# roles SQL above, which covers dbt's write path once tables/MVs exist in
# this database. What's missing — and StarRocks-specific — is that creating
# new tables/materialized views requires its own CREATE TABLE / CREATE
# MATERIALIZED VIEW privilege, granted at the database level, distinct from
# the table-level DML privileges above.
_b2b_analytics_db_sql = """\
CREATE DATABASE IF NOT EXISTS b2b_analytics;
GRANT CREATE TABLE ON DATABASE b2b_analytics TO ROLE app;
GRANT CREATE MATERIALIZED VIEW ON DATABASE b2b_analytics TO ROLE app;"""

_b2b_analytics_db_drop_sql = "DROP DATABASE IF EXISTS b2b_analytics;"

command.local.Command(
    f"starrocks-{stack_info.env_suffix}-b2b-analytics-database-setup",
    create=_exec_sql,
    update=_exec_sql,
    delete=_exec_delete_sql,
    environment={
        **_mysql_env,
        "STARROCKS_SQL": _b2b_analytics_db_sql,
        "STARROCKS_DELETE_SQL": _b2b_analytics_db_drop_sql,
    },
    triggers=[hashlib.sha256(_b2b_analytics_db_sql.encode()).hexdigest()],
    opts=ResourceOptions(
        delete_before_replace=True,
        depends_on=[roles_setup_cmd],
    ),
)

# --- OIDC / OAuth2 authentication via Keycloak ------------------------------
# StarRocks v3.5+ uses a "security integration" (SQL object) to hold OAuth2
# provider settings rather than fe.conf env vars.  The integration name goes
# directly in the authentication_chain FE config.
#
# The CREATE SECURITY INTEGRATION SQL is built at runtime from Vault-stored
# credentials (client_id / client_secret) so the secret is never stored
# in Pulumi state as plaintext — it is tainted as a Pulumi secret and
# encrypted by the stack's AWS KMS secrets provider.
#
# Both create and update commands use DROP IF EXISTS + CREATE so that
# property changes (e.g. a client secret rotation) are applied correctly.
# StarRocks has no ALTER SECURITY INTEGRATION for OAuth2 core properties.
#
# Users authenticate via the browser-based OAuth2 Authorization Code flow.
# Each human user must also be pre-created in StarRocks with:
#   CREATE USER 'preferred_username'@'%' IDENTIFIED WITH authentication_oauth2;
# and assigned an appropriate role.  Use starrocks:oidc_users in the stack
# config to manage these accounts through Pulumi; users not listed there
# must be created manually after the cluster is deployed.
#
# Service-account (application) access continues to use Vault dynamic
# credentials (native StarRocks auth) and is unaffected by OIDC config.
_OIDC_SECURITY_INTEGRATION_NAME = "keycloak_oauth2"
_JWT_SECURITY_INTEGRATION_NAME = "keycloak_jwt"

if oidc_enabled:
    # Read OIDC credentials from Vault. The client_secret is explicitly
    # marked as a Pulumi secret so any derived value is stored encrypted.
    _oidc_vault = vault_get_secret_output(
        path="secret-operations/sso/starrocks",
        with_lease_start_time=False,
        opts=InvokeOptions(provider=_vault_provider),
    )
    _oidc_issuer_url: pulumi.Output[str] = _oidc_vault.data.apply(lambda d: d["url"])
    _oidc_client_id: pulumi.Output[str] = _oidc_vault.data.apply(
        lambda d: d["client_id"]
    )
    _oidc_client_secret: pulumi.Output[str] = pulumi.Output.secret(
        _oidc_vault.data.apply(lambda d: d["client_secret"])
    )
    # FE OAuth2 callback endpoint; the FE HTTP port (8030) is proxied through
    # APISIX and exposed at the domain root.
    _oidc_redirect_url = f"https://{starrocks_config.require('domain')}/api/oauth2"

    def _build_integration_sql(args: list[str]) -> str:
        # json.dumps()[1:-1] produces a backslash-escaped string body safe for
        # embedding inside a SQL double-quoted string literal.  This guards
        # against values that contain double-quotes or backslashes (e.g. a
        # rotated client_secret generated with special characters).
        issuer = json.dumps(args[0])[1:-1]
        client_id = json.dumps(args[1])[1:-1]
        client_secret = json.dumps(args[2])[1:-1]
        _n = _OIDC_SECURITY_INTEGRATION_NAME
        return (
            f"CREATE SECURITY INTEGRATION {_n} PROPERTIES (\n"
            '    "type" = "authentication_oauth2",\n'
            f'    "auth_server_url" = "{issuer}'
            '/protocol/openid-connect/auth",\n'
            f'    "token_server_url" = "{issuer}'
            '/protocol/openid-connect/token",\n'
            f'    "client_id" = "{client_id}",\n'
            f'    "client_secret" = "{client_secret}",\n'
            f'    "redirect_url" = "{_oidc_redirect_url}",\n'
            f'    "jwks_url" = "{issuer}'
            '/protocol/openid-connect/certs",\n'
            '    "principal_field" = "starrocks_username",\n'
            '    "auto_provision_user" = "true",\n'
            f'    "required_issuer" = "{issuer}"\n'
            ");"
        )

    _integration_sql: pulumi.Output[str] = pulumi.Output.all(
        _oidc_issuer_url, _oidc_client_id, _oidc_client_secret
    ).apply(_build_integration_sql)
    _drop_integration_sql = (
        f"DROP SECURITY INTEGRATION {_OIDC_SECURITY_INTEGRATION_NAME};"
    )

    # StarRocks does not support DROP SECURITY INTEGRATION IF EXISTS, so the
    # create/update step silences the drop error (expected on first deploy when
    # the integration does not yet exist) then unconditionally runs the CREATE.
    # The delete step uses _exec_delete_sql which propagates errors normally.
    _exec_integration_sql = (
        f"{_mysql_opts_setup}"
        f" && {{ printf 'DROP SECURITY INTEGRATION {_OIDC_SECURITY_INTEGRATION_NAME};'"
        f" | {_mysql_client} 2>/dev/null || true; }}"
        f" && printf '%s' \"$STARROCKS_SQL\" | {_mysql_client}"
        f"; {_mysql_opts_cleanup}"
    )

    oauth2_security_integration_cmd = command.local.Command(
        f"starrocks-{stack_info.env_suffix}-security-integration-setup",
        create=_exec_integration_sql,
        update=_exec_integration_sql,
        delete=_exec_delete_sql,
        environment={
            **_mysql_env,
            "STARROCKS_SQL": _integration_sql,
            "STARROCKS_DELETE_SQL": _drop_integration_sql,
        },
        triggers=_integration_sql.apply(
            lambda sql: [hashlib.sha256(sql.encode()).hexdigest()]
        ),
        opts=ResourceOptions(
            delete_before_replace=True,
            depends_on=[starrocks_db_connection],
        ),
    )

    # JWT Security Integration — authentication_jwt (client-plugin flow).
    # The MySQL client acquires the id_token independently (e.g. via the
    # starrocks-auth PKCE script) and sends it over the MySQL wire using
    # the authentication_openid_connect_client plugin (MySQL 9.2+).
    # StarRocks verifies the id_token signature against the JWKS and maps
    # starrocks_username (the local part of preferred_username, without @domain)
    # to the StarRocks identity.
    #
    # This coexists with keycloak_oauth2 so that:
    #   - Web UI users: keycloak_oauth2 (browser OAuth2 redirect)
    #   - mysql CLI users: keycloak_jwt (pre-fetched id_token via starrocks-auth)
    # The id_token stored on the connection context is also forwarded to
    # Iceberg REST catalogs when iceberg.catalog.security = JWT, enabling
    # per-user identity delegation to the catalog.
    def _build_jwt_integration_sql(args: list[str]) -> str:
        issuer = json.dumps(args[0])[1:-1]
        _n = _JWT_SECURITY_INTEGRATION_NAME
        return (
            f"CREATE SECURITY INTEGRATION {_n} PROPERTIES (\n"
            '    "type" = "authentication_jwt",\n'
            f'    "jwks_url" = "{issuer}/protocol/openid-connect/certs",\n'
            '    "principal_field" = "starrocks_username",\n'
            '    "auto_provision_user" = "true",\n'
            f'    "required_issuer" = "{issuer}"\n'
            ");"
        )

    _jwt_integration_sql: pulumi.Output[str] = _oidc_issuer_url.apply(
        lambda issuer: _build_jwt_integration_sql([issuer])
    )
    _drop_jwt_integration_sql = (
        f"DROP SECURITY INTEGRATION {_JWT_SECURITY_INTEGRATION_NAME};"
    )
    _exec_jwt_integration_sql = (
        f"{_mysql_opts_setup}"
        f" && {{ printf 'DROP SECURITY INTEGRATION {_JWT_SECURITY_INTEGRATION_NAME};'"
        f" | {_mysql_client} 2>/dev/null || true; }}"
        f" && printf '%s' \"$STARROCKS_SQL\" | {_mysql_client}"
        f"; {_mysql_opts_cleanup}"
    )
    jwt_security_integration_cmd = command.local.Command(
        f"starrocks-{stack_info.env_suffix}-jwt-security-integration-setup",
        create=_exec_jwt_integration_sql,
        update=_exec_jwt_integration_sql,
        delete=_exec_delete_sql,
        environment={
            **_mysql_env,
            "STARROCKS_SQL": _jwt_integration_sql,
            "STARROCKS_DELETE_SQL": _drop_jwt_integration_sql,
        },
        triggers=_jwt_integration_sql.apply(
            lambda sql: [hashlib.sha256(sql.encode()).hexdigest()]
        ),
        opts=ResourceOptions(
            delete_before_replace=True,
            depends_on=[starrocks_db_connection],
        ),
    )

    # Point the FE authentication chain at both security integrations.
    # keycloak_oauth2: web UI browser-redirect OAuth2 flow
    # keycloak_jwt:    mysql CLI client-plugin flow (id_token pre-fetched)
    # StarRocks persists ADMIN SET FRONTEND CONFIG changes to BDB so they
    # survive pod restarts without requiring a fe.conf change.
    _auth_chain = (
        f"{_OIDC_SECURITY_INTEGRATION_NAME},{_JWT_SECURITY_INTEGRATION_NAME},native"
    )
    _auth_chain_sql = (
        f'ADMIN SET FRONTEND CONFIG ("authentication_chain" = "{_auth_chain}");'
    )
    _auth_chain_disable_sql = (
        'ADMIN SET FRONTEND CONFIG ("authentication_chain" = "native");'
    )
    command.local.Command(
        f"starrocks-{stack_info.env_suffix}-oidc-auth-chain-setup",
        create=_exec_sql,
        update=_exec_sql,
        delete=_exec_delete_sql,
        environment={
            **_mysql_env,
            "STARROCKS_SQL": _auth_chain_sql,
            "STARROCKS_DELETE_SQL": _auth_chain_disable_sql,
        },
        triggers=[hashlib.sha256(_auth_chain_sql.encode()).hexdigest()],
        opts=ResourceOptions(
            delete_before_replace=True,
            depends_on=[oauth2_security_integration_cmd, jwt_security_integration_cmd],
        ),
    )

    # --- File group provider: automatic role assignment from Keycloak --------
    # keycloak_group_sync.py calls the Keycloak Admin API (using the
    # ol-starrocks-client service account, which has view-users on
    # realm-management) to enumerate current members of each governance role
    # and writes the result to a Kubernetes ConfigMap mounted in the FE pods.
    # StarRocks' file group provider reads that file; combined with
    # GRANT role TO EXTERNAL GROUP, any OAuth2-authenticated user whose
    # preferred_username appears in the file receives the corresponding
    # StarRocks role automatically — no starrocks:oidc_users entry needed.
    #
    # Upstream feature request for a native JWT-claims group provider that
    # would eliminate the sync entirely:
    # https://github.com/StarRocks/starrocks/issues/75224
    _sync_script = str(Path(__file__).parent / "keycloak_group_sync.py")
    _group_provider_name = "keycloak_file_groups"
    _group_file_cm = f"{stack_info.env_prefix}-starrocks-oidc-groups"
    _group_file_path = "groups/groups.txt"
    _k8s_namespace = "starrocks"

    # Write the kubeconfig to a temp file so kubectl can reach the cluster.
    # The Concourse worker does not have a default kubeconfig for the data
    # EKS cluster; we pull the config from the cluster stack reference and
    # inject it via KUBECONFIG rather than relying on ambient credentials.
    _group_sync_run = (
        "_kf=$(mktemp)"
        ' && printf \'%s\' "$KUBECONFIG_CONTENT" > "$_kf"'
        f' && KUBECONFIG="$_kf" python3 {_sync_script}'
        f" --namespace={_k8s_namespace}"
        f" --configmap={_group_file_cm}"
        '; _rc=$?; rm -f "$_kf"; exit $_rc'
    )
    group_sync_cmd = command.local.Command(
        f"starrocks-{stack_info.env_suffix}-keycloak-group-sync",
        create=_group_sync_run,
        update=_group_sync_run,
        # On destroy the ConfigMap is owned and deleted by the applications stack;
        # no kubectl delete needed here.
        environment={
            "KEYCLOAK_ISSUER_URL": _oidc_issuer_url,
            "KEYCLOAK_CLIENT_ID": _oidc_client_id,
            "KEYCLOAK_CLIENT_SECRET": _oidc_client_secret,
            "KUBECONFIG_CONTENT": _kube_config,
        },
        triggers=_integration_sql.apply(
            lambda sql: [hashlib.sha256(sql.encode()).hexdigest()]
        ),
        opts=ResourceOptions(
            delete_before_replace=True,
            depends_on=[oauth2_security_integration_cmd],
        ),
    )

    _permitted = ",".join(sorted(_GOVERNANCE_ROLES))
    _group_provider_setup_sql = (
        f"CREATE GROUP PROVIDER IF NOT EXISTS {_group_provider_name}\n"
        f"PROPERTIES (\n"
        f'    "type" = "file",\n'
        f'    "group_file_url" = "{_group_file_path}"\n'
        f");\n"
        # Attach the group provider to both Security Integrations so that
        # role assignment from Keycloak groups works regardless of whether
        # the user authenticated via the web UI (keycloak_oauth2) or the
        # mysql CLI client-plugin flow (keycloak_jwt).
        f"ALTER SECURITY INTEGRATION {_OIDC_SECURITY_INTEGRATION_NAME}\n"
        f"    SET ('group_provider' = '{_group_provider_name}',"
        f" 'permitted_groups' = '{_permitted}');\n"
        f"ALTER SECURITY INTEGRATION {_JWT_SECURITY_INTEGRATION_NAME}\n"
        f"    SET ('group_provider' = '{_group_provider_name}',"
        f" 'permitted_groups' = '{_permitted}');\n"
        + "\n".join(
            f"GRANT {role} TO EXTERNAL GROUP {role};"
            for role in sorted(_GOVERNANCE_ROLES)
        )
    )
    _group_provider_drop_sql = (
        "\n".join(
            f"REVOKE {role} FROM EXTERNAL GROUP {role};"
            for role in sorted(_GOVERNANCE_ROLES)
        )
        + f"\nDROP GROUP PROVIDER IF EXISTS {_group_provider_name};"
    )

    command.local.Command(
        f"starrocks-{stack_info.env_suffix}-group-provider-setup",
        create=_exec_sql,
        update=_exec_sql,
        delete=_exec_delete_sql,
        environment={
            **_mysql_env,
            "STARROCKS_SQL": _group_provider_setup_sql,
            "STARROCKS_DELETE_SQL": _group_provider_drop_sql,
        },
        triggers=[hashlib.sha256(_group_provider_setup_sql.encode()).hexdigest()],
        opts=ResourceOptions(
            delete_before_replace=True,
            # roles_setup_cmd: GRANT <role> TO EXTERNAL GROUP requires the roles
            # to exist first.
            depends_on=[
                roles_setup_cmd,
                oauth2_security_integration_cmd,
                jwt_security_integration_cmd,
                group_sync_cmd,
            ],
        ),
    )

    # Pre-create OIDC-authenticated user accounts from config.
    # With auto_provision_user=true and the file group provider in place,
    # accounts and roles are normally handled automatically on first login.
    # Entries here are only needed for edge-case overrides (e.g. a user who
    # needs a different role than the one assigned via their Keycloak groups).
    #
    # Each entry must supply:
    #   username    - the starrocks_username claim value (local part of the Keycloak
    #                 preferred_username, without @domain); whitespace is stripped,
    #                 empty strings are rejected
    #   keycloak_role - one of the six governance role names in _GOVERNANCE_ROLES
    #                   (ol_platform_admin / ol_data_engineer / ol_data_analyst /
    #                    ol_researcher / ol_instructor / ol_business_analyst)
    # The keycloak_role value matches the StarRocks role name exactly (1:1 mapping).
    # One Pulumi resource is created per user so that removing an entry from config
    # during `pulumi up` triggers the DROP USER for that specific account.
    # Users not listed here can be created manually:
    #   CREATE USER 'username'@'%' IDENTIFIED WITH authentication_jwt;
    #   GRANT <governance_role> TO USER 'username'@'%';
    #   ALTER USER 'username'@'%' DEFAULT ROLE <governance_role>;
    oidc_users: list[dict[str, str]] = starrocks_config.get_object("oidc_users") or []
    for _u in oidc_users:
        _username = _u.get("username", "").strip()
        _role = _u.get("keycloak_role", "").strip()
        if not _username:
            msg = "starrocks:oidc_users entry is missing or has a blank 'username'"
            raise ValueError(msg)
        if not _OIDC_USERNAME_RE.match(_username):
            msg = (
                f"starrocks:oidc_users entry has an invalid username '{_username}'."
                " Usernames may only contain letters, digits, and the characters"
                " . _ % + @ - (no quotes, whitespace, or control characters)."
            )
            raise ValueError(msg)
        if _role not in _GOVERNANCE_ROLES:
            msg = (
                f"starrocks:oidc_users entry for '{_username}' has"
                f" invalid keycloak_role '{_role}'."
                f" Must be one of: {sorted(_GOVERNANCE_ROLES)}"
            )
            raise ValueError(msg)
        # DROP + CREATE (rather than CREATE IF NOT EXISTS) so that re-running with
        # a different config corrects an account that was previously created with
        # a different auth plugin (e.g. a native-password user being migrated to
        # OAuth2).  The username is validated against _OIDC_USERNAME_RE above, so
        # it is safe to interpolate into these single-quoted SQL literals.
        _user_sql = (
            f"DROP USER IF EXISTS '{_username}'@'%';"
            f"\nCREATE USER '{_username}'@'%'"
            f" IDENTIFIED WITH authentication_jwt;"
            f"\nGRANT {_role} TO USER '{_username}'@'%';"
            f"\nALTER USER '{_username}'@'%' DEFAULT ROLE {_role};"
        )
        _user_drop_sql = f"DROP USER IF EXISTS '{_username}'@'%';"
        # Resource name: a sanitized username for human readability plus a short
        # stable hash of the full username so that two usernames that sanitize to
        # the same string (e.g. "a.b@x" and "a-b@x") cannot collide.
        _sanitized = re.sub(r"[^A-Za-z0-9-]", "-", _username)
        _name_hash = hashlib.sha256(_username.encode()).hexdigest()[:8]
        command.local.Command(
            f"starrocks-{stack_info.env_suffix}-oidc-user-{_sanitized}-{_name_hash}",
            create=_exec_sql,
            update=_exec_sql,
            delete=_exec_delete_sql,
            environment={
                **_mysql_env,
                "STARROCKS_SQL": _user_sql,
                "STARROCKS_DELETE_SQL": _user_drop_sql,
            },
            triggers=[hashlib.sha256(_user_sql.encode()).hexdigest()],
            opts=ResourceOptions(
                delete_before_replace=True,
                # roles_setup_cmd: GRANT <role> / DEFAULT ROLE require the role to
                # exist before user creation runs on first deploy.
                depends_on=[
                    roles_setup_cmd,
                    oauth2_security_integration_cmd,
                    jwt_security_integration_cmd,
                ],
            ),
        )

export("vault_mount_path", mount_point)
