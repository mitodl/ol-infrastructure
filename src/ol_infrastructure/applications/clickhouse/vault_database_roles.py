"""Statements Vault's database secrets engine runs on ClickHouse for each role.

Users created over SQL live in each replica's own access storage
(user_directories is users_xml plus local_directory), so every statement runs
ON CLUSTER. Without it a credential would exist only on the replica the NLB
happened to pick, and revocation would leave copies behind.

Vault substitutes {{name}} and {{password}}. '{cluster}' is a ClickHouse macro
that the operator defines on every replica, and Vault leaves single braces
alone.
"""

ON_CLUSTER = "ON CLUSTER '{cluster}'"
OPIK_DATABASE = "opik_db"
OPIK_CUTOVER_ROLE = "opik-cutover"

# What vault_admin must hold WITH GRANT OPTION to issue every role here, plus
# the user management statements themselves. ClickHouse refuses a users.xml
# user that sets both access_management and grants, so these grants are the
# whole of its access. Widen them when a role needs more.
VAULT_ADMIN_GRANTS = (
    "GRANT CREATE USER, ALTER USER, DROP USER ON *.*",
    "GRANT CLUSTER, REMOTE ON *.* WITH GRANT OPTION",
    "GRANT SELECT ON system.* WITH GRANT OPTION",
    "GRANT SELECT, INSERT, CREATE TABLE, DROP TABLE, ALTER DELETE, ALTER UPDATE"
    f" ON {OPIK_DATABASE}.* WITH GRANT OPTION",
)

REVOCATION_STATEMENTS = [f"DROP USER IF EXISTS `{{{{name}}}}` {ON_CLUSTER};"]
# No rollback statements: vault-plugin-database-starrocks never runs them
# (dbplugin v5 leaves that to the plugin), so a grant that fails after CREATE
# USER leaves the user behind with no lease. Nobody was handed its password;
# drop it by hand with the revocation statement above.

# Upstream's traces/spans v2 cutover (apps/opik-backend/data-migrations/
# {traces,spans}-local-v2-cutover/README.md, "Required privileges"), forward
# path only. EXCHANGE and RENAME check SELECT, INSERT, CREATE TABLE and DROP
# TABLE on every name they touch, so each name gets all four. A lightweight
# DELETE on the live table (reconcile.sh) and the shadow (deletion replay)
# checks ALTER DELETE and, because it is ALTER UPDATE _row_exists = 0, that
# column's ALTER UPDATE. The traces README lists only the second; 26.3 refuses
# the DELETE without the first. Column-scoping the update means the user can
# mask rows but not rewrite a real column. No TRUNCATE: rollback and finalize
# grants are a separate role once those steps are in scope.
_CUTOVER_TABLES = ("traces", "spans")


def _create_user(client_networks: list[str]) -> str:
    hosts = ", ".join(f"IP '{cidr}'" for cidr in client_networks)
    return (
        f"CREATE USER `{{{{name}}}}` {ON_CLUSTER}"
        f" IDENTIFIED WITH sha256_password BY '{{{{password}}}}' HOST {hosts};"
    )


def _grant(privileges: str, target: str) -> str:
    return f"GRANT {ON_CLUSTER} {privileges} ON {target} TO `{{{{name}}}}`;"


def opik_cutover_creation_statements(client_networks: list[str]) -> list[str]:
    """Return the statements that create an opik-cutover user.

    :param client_networks: CIDRs the issued user may log in from.
    :returns: CREATE USER followed by its grants, in execution order.
    :rtype: list[str]
    """
    grants: list[str] = []
    for table in _CUTOVER_TABLES:
        grants.extend(
            _grant(
                "SELECT, INSERT, CREATE TABLE, DROP TABLE", f"{OPIK_DATABASE}.{name}"
            )
            for name in (table, f"{table}_local_v2", f"{table}_pre_cutover_backup")
        )
        grants.extend(
            _grant("ALTER DELETE, ALTER UPDATE(_row_exists)", f"{OPIK_DATABASE}.{name}")
            for name in (table, f"{table}_local_v2")
        )
    # estimate.sh, the guards and the settle gate read system tables across
    # replicas with clusterAllReplicas().
    grants.append(_grant("SELECT", "system.*"))
    grants.append(_grant("REMOTE, CLUSTER", "*.*"))
    return [_create_user(client_networks), *grants]
