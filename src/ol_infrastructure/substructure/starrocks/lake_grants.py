"""StarRocks GRANTs and REVOKEs that scope governance roles to data lake layers.

Every Iceberg catalog over the lake, Glue or Gravitino, renders its governance
role grants here from ``data_lake_access``, the mapping Gravitino's own grants
are built from. A catalog-wide grant on a Glue catalog is a way around
Gravitino's per-user enforcement, since the Glue catalogs answer every user
with the FE's IRSA role.

StarRocks GRANTs are additive, so narrowing the mapping does nothing to a role
that already holds a grant. ``layer_scoped_revokes_sql`` revokes everything a
layer-scoped role may not hold. StarRocks 4.1.7 has no REVOKE IF EXISTS and
fails a REVOKE of a privilege the role does not hold, so those statements run
through ``revoke_step``, which tolerates exactly that error.

A per-database grant on an external catalog stores the database name without
resolving it, so none of this needs the catalog to be reachable, and a grant
naming a layer database that does not exist yet takes effect once it does.
"""

from ol_infrastructure.lib.data_lake_access import (
    CATALOG_WIDE_WRITE_ROLES,
    DATA_LAKE_LAYERS,
    GOVERNANCE_LAYER_ACCESS,
    layer_database,
)

# What StarRocks 4.1.7 says when a REVOKE names a privilege the role lacks, e.g.
# "ERROR 1064 (HY000) at line 3: There is no such grant defined on ALL TABLES IN
# ALL DATABASES".
NO_SUCH_GRANT_ERROR = "There is no such grant defined on"


def _in_catalog(catalog: str, statements: list[str]) -> list[str]:
    return [f"SET CATALOG {catalog};", *statements, "SET CATALOG default_catalog;"]


def layer_scoped_grants_sql(catalog: str, env_suffix: str) -> str:
    """Render SELECT on each layer a layer-scoped governance role may read.

    SELECT only, whatever ``data_lake_access`` says: no layer-scoped role is
    given ``write`` today, and a write through the Gravitino catalog would
    commit as its bootstrap credential rather than the user. A role the mapping
    gives no layer gets no USAGE either.

    :param catalog: The StarRocks catalog name.
    :param env_suffix: The environment suffix of the lake the catalog reads,
        e.g. ``qa``.

    :returns: The statements, switching back to default_catalog after each role.
    :rtype: str
    """
    statements: list[str] = []
    for role, layers in GOVERNANCE_LAYER_ACCESS.items():
        if not layers:
            continue
        statements.append(f"GRANT USAGE ON CATALOG {catalog} TO ROLE {role};")
        statements.extend(
            _in_catalog(
                catalog,
                [
                    "GRANT SELECT ON ALL TABLES IN DATABASE "
                    f"{layer_database(env_suffix, layer)} TO ROLE {role};"
                    for layer in layers
                ],
            )
        )
    return "\n".join(statements)


def governance_grants_sql(catalog: str, env_suffix: str) -> str:
    """Render SELECT grants for every governance role on a read-only catalog.

    The catalog-wide roles read the whole catalog and the rest read their
    layers. Catalogs a role may also write, i.e. an environment's own Glue
    catalog, grant the catalog-wide roles more than this, so they call
    ``layer_scoped_grants_sql`` instead.

    SELECT-only does not hold for roles that also hold the built-in
    ``db_admin`` (ol_platform_admin, ol_data_engineer): it carries every
    privilege on every catalog, external ones included, and cannot be revoked
    per catalog. A write by one of them through the Gravitino catalog is
    planned, writes data files with the BE's own S3 access, and then fails at
    commit because the bootstrap principal holds no Gravitino grants, leaving
    orphan files. Both roles already write through the Glue catalog, so this is
    no new data access. db_admin also lets them DROP the Gravitino catalog,
    which drops every grant on it and is not re-created until a trigger
    changes, or ALTER its URI or token endpoint, which redirects the credential
    and every user's token.

    :param catalog: The StarRocks catalog name.
    :param env_suffix: The environment suffix of the lake the catalog reads,
        e.g. ``qa``.

    :returns: The statements, switching back to default_catalog after each role.
    :rtype: str
    """
    statements: list[str] = []
    for role in CATALOG_WIDE_WRITE_ROLES:
        statements.append(f"GRANT USAGE ON CATALOG {catalog} TO ROLE {role};")
        statements.extend(
            _in_catalog(
                catalog,
                [f"GRANT SELECT ON ALL TABLES IN ALL DATABASES TO ROLE {role};"],
            )
        )
    statements.append(layer_scoped_grants_sql(catalog, env_suffix))
    return "\n".join(statements)


def layer_scoped_revokes_sql(catalog: str, env_suffix: str) -> str:
    """Render REVOKEs of every grant a layer-scoped role may not hold.

    That is SELECT on all databases, which the Glue catalogs granted every
    governance role before they were scoped and which also covers every
    developer's dbt schemas; SELECT on each layer the mapping does not give the
    role; and USAGE on the catalog for a role given no layer. Most of these
    name a grant the role does not hold, so run them with ``revoke_step``.

    :param catalog: The StarRocks catalog name.
    :param env_suffix: The environment suffix of the lake the catalog reads,
        e.g. ``qa``.

    :returns: The statements, switching back to default_catalog after each role.
    :rtype: str
    """
    # S608 reads "FROM ROLE" as a query. Every interpolation is a role name from
    # data_lake_access or a database layer_database validated.
    statements: list[str] = []
    for role, layers in GOVERNANCE_LAYER_ACCESS.items():
        statements.extend(
            _in_catalog(
                catalog,
                [
                    f"REVOKE SELECT ON ALL TABLES IN ALL DATABASES FROM ROLE {role};",  # noqa: S608
                    *(
                        "REVOKE SELECT ON ALL TABLES IN DATABASE "  # noqa: S608
                        f"{layer_database(env_suffix, layer)} FROM ROLE {role};"
                        for layer in DATA_LAKE_LAYERS
                        if layer not in layers
                    ),
                ],
            )
        )
        if not layers:
            statements.append(f"REVOKE USAGE ON CATALOG {catalog} FROM ROLE {role};")
    return "\n".join(statements)


def revoke_step(mysql_client: str, sql_env_var: str) -> str:
    """Render a shell step that runs REVOKEs, failing only on unexpected errors.

    ``--force`` carries on past each failing statement and makes the client
    exit 0 whatever happened, so the step reads the errors itself and fails on
    any but ``NO_SUCH_GRANT_ERROR``, which includes not being able to connect.

    :param mysql_client: The client invocation, reading SQL from stdin.
    :param sql_env_var: The environment variable holding the REVOKEs.

    :returns: A brace group that exits non-zero on an unexpected error.
    :rtype: str
    """
    return (
        f"{{ _revoke_errors=$(printf '%s' \"${sql_env_var}\""
        f" | {mysql_client} --force 2>&1 >/dev/null"
        f" | grep '^ERROR' | grep -v '{NO_SUCH_GRANT_ERROR}');"
        ' [ -z "$_revoke_errors" ]'
        " || { printf '%s\\n' \"$_revoke_errors\" >&2; false; }; }"
    )
