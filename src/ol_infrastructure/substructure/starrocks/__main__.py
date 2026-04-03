"""Configure Vault dynamic database credentials for StarRocks.

StarRocks exposes a MySQL-compatible protocol on port 9030 (the FE query port).
Vault uses the MySQL database plugin to manage dynamic credentials.

StarRocks privilege SQL differs from standard MySQL:
  - GRANT uses the StarRocks model: GRANT priv ON ALL TABLES IN ...
  - DROP USER uses 'user'@'host' format
  - Catalog-level grants needed for Iceberg catalog access

Connectivity: Vault must be able to reach the StarRocks FE MySQL port (9030)
over the VPC. Configure starrocks:fe_host to an internally routable address
(e.g. an internal NLB DNS name or a stable VPC-accessible hostname).
"""

import pulumi_vault as vault
from ol_infrastructure.lib.magic_numbers import ONE_MONTH_SECONDS
from pulumi import Config, ResourceOptions, StackReference, export

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
connection_url = db_host.apply(
    lambda host: f"{{{{username}}}}:{{{{password}}}}@tcp({host}:{db_port})/"
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

starrocks_db_connection = vault.database.SecretBackendConnection(
    f"starrocks-{stack_info.env_suffix}-vault-database-connection",
    backend=starrocks_vault_mount.path,
    name="starrocks",
    verify_connection=starrocks_config.get_bool("verify_connection") or False,
    allowed_roles=sorted(starrocks_role_statements.keys()),
    data={
        "username": db_admin_username,
        "password": db_admin_password,
    },
    # Use the plain mysql plugin (not mysql_rds) — StarRocks is not on RDS.
    # No tls_ca since this is an internal VPC connection.
    mysql={
        "connection_url": connection_url,
        "username_template": '{{printf "v-%.8s-%.8s-%.20s" (.DisplayName) (.RoleName) (random 20) | truncate 32}}',  # noqa: E501
    },
    opts=ResourceOptions(
        parent=starrocks_vault_mount,
        delete_before_replace=True,
        depends_on=[starrocks_vault_mount],
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
