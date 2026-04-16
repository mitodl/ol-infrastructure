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

import pulumi
import pulumi_command as command
import pulumi_vault as vault
from pulumi import Config, ResourceOptions, StackReference, export

from bridge.lib.magic_numbers import ONE_MONTH_SECONDS
from bridge.lib.versions import VAULT_PLUGIN_STARROCKS_SHA256
from ol_infrastructure.lib.pulumi_helper import parse_stack
from ol_infrastructure.lib.vault import setup_vault_provider

setup_vault_provider()
stack_info = parse_stack()
starrocks_config = Config("starrocks")

env_name = f"data-{stack_info.env_suffix}"
mount_point = f"database-starrocks-{stack_info.env_suffix}"

STARROCKS_MYSQL_PORT = 9030
MAX_TTL = ONE_MONTH_SECONDS * 6
DEFAULT_TTL = ONE_MONTH_SECONDS * 3

# Read the FE MySQL NLB hostname and admin password from the applications stack so
# both values stay in sync with the deployed cluster without duplication.
applications_stack = StackReference(starrocks_config.require("applications_stack_name"))
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

# --- Iceberg catalog --------------------------------------------------------
catalog_setup: command.local.Command | None = None
if enable_data_lake:
    CATALOG_NAME = f"ol_data_lake_{stack_info.env_suffix}"
    aws_region = starrocks_config.get("aws_region") or "us-east-1"

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
    _catalog_conn_props = (
        f'    "aws.glue.use_instance_profile" = "false",\n'
        f'    "aws.glue.region" = "{aws_region}",\n'
        f'    "aws.s3.use_instance_profile" = "false",\n'
        f'    "aws.s3.region" = "{aws_region}"'
    )
    _catalog_sql = (
        f"CREATE EXTERNAL CATALOG IF NOT EXISTS {CATALOG_NAME}\n"
        f"COMMENT 'MIT OL Data Lake {stack_info.name} Iceberg Catalog"
        f" (AWS Glue / {stack_info.name})'\n"
        f"PROPERTIES(\n"
        f'    "type" = "iceberg",\n'
        f'    "iceberg.catalog.type" = "glue",\n'
        f"{_catalog_conn_props}\n"
        f");"
    )

    catalog_setup = command.local.Command(
        f"starrocks-{stack_info.env_suffix}-catalog-setup",
        create=_exec_sql,
        update=_exec_sql,
        delete=_exec_delete_sql,
        environment={
            **_mysql_env,
            "STARROCKS_SQL": _catalog_sql,
            "STARROCKS_DELETE_SQL": f"DROP CATALOG IF EXISTS {CATALOG_NAME};",
        },
        triggers=[hashlib.sha256(_catalog_sql.encode()).hexdigest()],
        opts=ResourceOptions(depends_on=[starrocks_db_connection]),
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
CREATE ROLE IF NOT EXISTS readonly;
CREATE ROLE IF NOT EXISTS app;
CREATE ROLE IF NOT EXISTS admin;

GRANT USAGE ON CATALOG default_catalog TO ROLE readonly;
GRANT SELECT ON ALL TABLES IN ALL DATABASES TO ROLE readonly;

GRANT USAGE ON CATALOG default_catalog TO ROLE app;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN ALL DATABASES TO ROLE app;

GRANT cluster_admin TO ROLE admin;
GRANT db_admin TO ROLE admin;
GRANT user_admin TO ROLE admin;
GRANT USAGE ON CATALOG default_catalog TO ROLE admin;
GRANT ALL ON ALL TABLES IN ALL DATABASES TO ROLE admin;
GRANT ALL ON ALL DATABASES TO ROLE admin;"""

_iceberg_roles_sql = f"""
GRANT USAGE ON CATALOG {CATALOG_NAME} TO ROLE readonly;
SET CATALOG {CATALOG_NAME};
GRANT SELECT ON ALL TABLES IN ALL DATABASES TO ROLE readonly;
SET CATALOG default_catalog;

GRANT USAGE ON CATALOG {CATALOG_NAME} TO ROLE app;
SET CATALOG {CATALOG_NAME};
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN ALL DATABASES TO ROLE app;
SET CATALOG default_catalog;

GRANT USAGE ON CATALOG {CATALOG_NAME} TO ROLE admin;
SET CATALOG {CATALOG_NAME};
GRANT ALL ON ALL TABLES IN ALL DATABASES TO ROLE admin;
SET CATALOG default_catalog;"""

_roles_sql = _base_roles_sql + (_iceberg_roles_sql if enable_data_lake else "")
_role_deps: list[pulumi.Resource] = [starrocks_db_connection]
if catalog_setup is not None:
    _role_deps.append(catalog_setup)

_roles_drop_sql = (
    "DROP ROLE IF EXISTS readonly; DROP ROLE IF EXISTS app; DROP ROLE IF EXISTS admin;"
)

command.local.Command(
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
    opts=ResourceOptions(depends_on=_role_deps),
)

# --- OIDC authentication chain ----------------------------------------------
# Sets the StarRocks FE runtime authentication chain to include OIDC when
# oidc_enabled=true.  This is a runtime FE config change; for persistence
# across FE restarts the applications stack Helm values must also set
# authentication_chain in fe.conf (otherwise the change reverts on restart).
#
# Authorization: StarRocks uses its native privilege model for OIDC-
# authenticated sessions the same as for password-authenticated ones.  OIDC
# users must be pre-created in StarRocks (CREATE USER ... IDENTIFIED WITH
# authentication_oidc) and assigned roles or grants before they can access
# data.  This substructure creates the role objects (see roles-setup above)
# but not the individual user-to-role mappings; those are managed separately.
if oidc_enabled:
    _auth_chain_enabled = "authentication_starrocks,authentication_oidc"
    _oidc_sql = (
        f'ADMIN SET FRONTEND CONFIG ("authentication_chain" = "{_auth_chain_enabled}");'
    )
    _oidc_disable_sql = (
        "ADMIN SET FRONTEND CONFIG"
        ' ("authentication_chain" = "authentication_starrocks");'
    )
    command.local.Command(
        f"starrocks-{stack_info.env_suffix}-oidc-setup",
        create=_exec_sql,
        update=_exec_sql,
        delete=_exec_delete_sql,
        environment={
            **_mysql_env,
            "STARROCKS_SQL": _oidc_sql,
            "STARROCKS_DELETE_SQL": _oidc_disable_sql,
        },
        triggers=[hashlib.sha256(_oidc_sql.encode()).hexdigest()],
        opts=ResourceOptions(depends_on=[starrocks_db_connection]),
    )

export("vault_mount_path", mount_point)
