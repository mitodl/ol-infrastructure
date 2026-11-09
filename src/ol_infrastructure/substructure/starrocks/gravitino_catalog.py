"""The StarRocks catalog people query the lake through, served by Gravitino.

The Glue catalogs (``ol_data_lake_<env>``) answer every StarRocks user with the
FE pod's own IRSA role, so StarRocks GRANTs are the only per-person control on
them. This catalog points at Gravitino's Iceberg REST service with
``iceberg.catalog.security = JWT``, which forwards each user's id_token, so
Gravitino's per-role grants apply as well. Machine access (Dagster, the
readonly / app / admin roles) stays on the Glue catalogs (deployment spec,
Option A). See docs/plans/gravitino-deployment-spec.md and
docs/plans/gravitino-glue-backend-spike-results.md.

The property names are the 4.1.7 FE's (``IcebergRESTCatalog`` and
``OAuth2SecurityConfig``), which strips the ``iceberg.catalog.`` prefix before
handing the rest to Iceberg's ``RESTSessionCatalog``.
"""

import re
from collections.abc import Mapping

from pydantic import BaseModel, ConfigDict, Field

from ol_infrastructure.lib.data_lake_access import (
    CATALOG_WIDE_WRITE_ROLES,
    GOVERNANCE_LAYER_ACCESS,
    GOVERNANCE_ROLES,
    layer_database,
)

CREDENTIAL_PROPERTY = "iceberg.catalog.oauth2.credential"
# Takes the credential's place in the SQL that is hashed into a Command trigger,
# so rotating the secret changes neither the trigger nor what state records.
CREDENTIAL_PLACEHOLDER = "<bootstrap credential>"


def catalog_name(env_suffix: str) -> str:
    """Return the StarRocks name of an environment's Gravitino catalog.

    :param env_suffix: The environment suffix, e.g. ``qa``.

    :returns: e.g. ``ol_governed_lake_qa``.
    :rtype: str
    """
    return f"ol_governed_lake_{env_suffix}"


_SAFE_LITERAL = re.compile(r"[ -~]*")


def _literal(value: str) -> str:
    # StarRocks unescapes only a few backslash sequences (AstBuilder
    # escapeBackSlash), so an escaped non-ASCII or control character would
    # arrive altered. Refuse those, and '"' and '\', instead of escaping them.
    if not _SAFE_LITERAL.fullmatch(value) or '"' in value or "\\" in value:
        msg = "catalog property values must be printable ASCII without '\"' or '\\'"
        raise ValueError(msg)
    return value


def _properties_sql(properties: Mapping[str, str]) -> str:
    return ",\n".join(
        f'    "{_literal(key)}" = "{_literal(value)}"'
        for key, value in properties.items()
    )


class GravitinoCatalog(BaseModel):
    """One StarRocks external catalog over a Gravitino Iceberg REST catalog."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(pattern=r"^\w+$")
    uri: str
    # Gravitino's dynamic-config-provider selects its catalog by this name.
    warehouse: str
    oauth2_server_uri: str
    aws_region: str

    def create_sql(self, credential: str) -> str:
        """Render the CREATE EXTERNAL CATALOG statement.

        The bootstrap credential has to be here, not only in ``alter_sql``:
        creating the connector initializes ``RESTSessionCatalog``, whose
        ``GET /v1/config`` carries no user session, and Gravitino refuses it
        unauthenticated. With ``security = JWT`` and no credential, StarRocks
        also drops every user's token (StarRocks#75792). The credential needs no
        Gravitino grants, and must hold none: StarRocks commits an INSERT through
        this catalog as the credential, not as the user.

        StarRocks 4.1.7 does not redact this property: CREATE and ALTER CATALOG
        write it in plaintext to fe.audit.log (SqlCredentialRedactor covers only
        PrintableMap.SENSITIVE_KEY), and CatalogMgr logs the raw config to
        fe.log on every ALTER that changes something, on each FE. Those files
        stay on the FE log volume (no AuditLoader, not shipped to Loki). That is
        tolerable only because the credential holds no Gravitino grants.

        :param credential: ``<client_id>:<client_secret>`` of the
            ol-gravitino-catalog Keycloak client, or ``CREDENTIAL_PLACEHOLDER``.

        :returns: The statement.
        :rtype: str
        """
        properties = {
            "type": "iceberg",
            "iceberg.catalog.type": "rest",
            "iceberg.catalog.uri": self.uri,
            "iceberg.catalog.warehouse": self.warehouse,
            "iceberg.catalog.security": "JWT",
            "iceberg.catalog.oauth2.server-uri": self.oauth2_server_uri,
            "iceberg.catalog.oauth2.scope": "openid",
            CREDENTIAL_PROPERTY: credential,
        }
        return (
            f"CREATE EXTERNAL CATALOG IF NOT EXISTS {self.name}\n"
            "COMMENT 'MIT OL Data Lake, with your own Gravitino access'\n"
            f"PROPERTIES(\n{_properties_sql(properties)}\n);"
        )

    def alter_sql(self, credential: str) -> str:
        """Render the ALTER CATALOG ... SET of the properties that may change.

        ALTER CATALOG swaps the connector in place and keeps the catalog, and
        with it every grant on it, where a changed CREATE would drop it.

        Vended credentials are on (the 4.1.7 default, set to say so), and
        ``aws.s3.use_aws_sdk_default_behavior`` is deliberately absent: with it,
        a table Gravitino failed to vend for would still be read with the
        StarRocks pods' IRSA role, hiding the failure. ``aws.s3.region`` only supplies
        the region when the vended credential does not.

        :param credential: As for ``create_sql``.

        :returns: The statement.
        :rtype: str
        """
        properties = {
            CREDENTIAL_PROPERTY: credential,
            "iceberg.catalog.vended-credentials-enabled": "true",
            "aws.s3.region": self.aws_region,
        }
        return f"ALTER CATALOG {self.name} SET (\n{_properties_sql(properties)}\n);"


def governance_grants_sql(catalog: str, env_suffix: str) -> str:
    """Render the StarRocks grants that let governance roles read the catalog.

    SELECT only. A write through this catalog would commit as the bootstrap
    credential rather than the user, so nobody gets INSERT, UPDATE or DELETE
    here, and readonly / app / admin get nothing. Scope comes from
    ``data_lake_access``, the same mapping Gravitino's grants are built from;
    a role it gives nothing gets no USAGE either.

    SELECT-only does not hold for roles that also hold the built-in
    ``db_admin`` (ol_platform_admin, ol_data_engineer): it carries every
    privilege on every catalog, external ones included, and cannot be revoked
    per catalog. A write by one of them is planned, writes data files with the
    BE's own S3 access, and then fails at commit because the bootstrap
    principal holds no Gravitino grants, leaving orphan files. Both roles
    already write through the Glue catalog, so this is no new access.

    A per-database grant on an external catalog stores the database name
    without resolving it, so these need no call to Gravitino, which the admin
    session making them could not authorize.

    :param catalog: The StarRocks catalog name.
    :param env_suffix: The environment suffix, e.g. ``qa``.

    :returns: The statements, switching back to default_catalog at the end.
    :rtype: str
    """
    statements: list[str] = []
    for role in GOVERNANCE_ROLES:
        if role in CATALOG_WIDE_WRITE_ROLES:
            targets = ["ALL TABLES IN ALL DATABASES"]
        else:
            targets = [
                f"ALL TABLES IN DATABASE {layer_database(env_suffix, layer)}"
                for layer in GOVERNANCE_LAYER_ACCESS[role]
            ]
        if not targets:
            continue
        statements.append(f"GRANT USAGE ON CATALOG {catalog} TO ROLE {role};")
        statements.append(f"SET CATALOG {catalog};")
        statements.extend(
            f"GRANT SELECT ON {target} TO ROLE {role};" for target in targets
        )
        statements.append("SET CATALOG default_catalog;")
    return "\n".join(statements)
