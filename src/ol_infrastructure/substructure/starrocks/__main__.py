"""Configure Vault dynamic database credentials for StarRocks.

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

StarRocks privilege SQL differs from standard MySQL:
  - GRANT uses the StarRocks model: GRANT priv ON ALL TABLES IN ...
  - DROP USER uses 'user'@'host' format
  - Catalog-level grants needed for Iceberg catalog access

Connectivity: Vault must be able to reach the StarRocks FE MySQL port (9030)
over the VPC. Configure starrocks:fe_host to an internally routable address
(e.g. an internal NLB DNS name or a stable VPC-accessible hostname).
"""

import json

import pulumi
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

# StarRocks-compatible SQL role statements.
# Each list entry is executed as a separate statement in the same connection.
#
# StarRocks privilege model differences from MySQL:
#  - Table grants: GRANT priv ON ALL TABLES IN ALL DATABASES TO USER 'u'@'h'
#  - Catalog grants: GRANT USAGE ON CATALOG <name> TO USER 'u'@'h'
#  - Drop: DROP USER 'u'@'%' (same syntax as MySQL)
starrocks_role_statements: dict[str, dict[str, list[str]]] = {
    "readonly": {
        "create": [
            "CREATE USER '{{name}}'@'%' IDENTIFIED BY '{{password}}';",
            # Grant access to query internal StarRocks tables
            "GRANT USAGE ON CATALOG default_catalog TO USER '{{name}}'@'%';",
            "GRANT SELECT ON ALL TABLES IN ALL DATABASES TO USER '{{name}}'@'%';",
            # Grant access to the Iceberg data lake catalog
            "GRANT USAGE ON CATALOG ol_data_lake_iceberg TO USER '{{name}}'@'%';",
        ],
        "revoke": ["DROP USER '{{name}}'@'%';"],
        "renew": [],
        "rollback": [],
    },
    "app": {
        "create": [
            "CREATE USER '{{name}}'@'%' IDENTIFIED BY '{{password}}';",
            "GRANT USAGE ON CATALOG default_catalog TO USER '{{name}}'@'%';",
            "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN ALL DATABASES TO USER '{{name}}'@'%';",  # noqa: E501
            "GRANT USAGE ON CATALOG ol_data_lake_iceberg TO USER '{{name}}'@'%';",
        ],
        "revoke": ["DROP USER '{{name}}'@'%';"],
        "renew": [],
        "rollback": [],
    },
    "admin": {
        "create": [
            "CREATE USER '{{name}}'@'%' IDENTIFIED BY '{{password}}';",
            # NODE_PRIV allows cluster management operations (ALTER SYSTEM, etc.)
            "GRANT NODE_PRIV ON *.* TO USER '{{name}}'@'%';",
            # Full DML + DDL on all databases in the default catalog
            "GRANT ALL ON ALL TABLES IN ALL DATABASES TO USER '{{name}}'@'%';",
            "GRANT ALL ON ALL DATABASES TO USER '{{name}}'@'%';",
            "GRANT USAGE ON CATALOG default_catalog TO USER '{{name}}'@'%';",
            "GRANT USAGE ON CATALOG ol_data_lake_iceberg TO USER '{{name}}'@'%';",
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

export("vault_mount_path", mount_point)
