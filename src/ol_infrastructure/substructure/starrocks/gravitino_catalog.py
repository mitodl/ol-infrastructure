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

CREDENTIAL_PROPERTY = "iceberg.catalog.oauth2.credential"
# Takes the credential's place in the SQL that is hashed into a Command trigger,
# so rotating the secret changes neither the trigger nor what state records.
CREDENTIAL_PLACEHOLDER = "<bootstrap credential>"
# Iceberg's HTTPClient builds this catalog's TLS context from the named
# TLSConfigurer (applications/starrocks files/CaFileTLSConfigurer.java), which
# adds the CA file to the JVM's default CAs for this client alone, so the FE JVM
# never has to trust the Gravitino CA.
TLS_CONFIGURER_PROPERTY = "iceberg.catalog.rest.client.tls.configurer-impl"
TLS_CA_FILE_PROPERTY = "iceberg.catalog.rest.client.tls.ca-file"


def catalog_name(env_suffix: str) -> str:
    """Return the StarRocks name of an environment's Gravitino catalog.

    :param env_suffix: The environment suffix, e.g. ``qa``.

    :returns: e.g. ``ol_governed_lake_qa``.
    :rtype: str
    """
    return f"ol_governed_lake_{env_suffix}"


_SAFE_LITERAL = re.compile(r"[ -~]+")


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
    tls_configurer_impl: str
    tls_ca_file: str

    def _identity_properties(self, credential: str) -> dict[str, str]:
        return {
            "type": "iceberg",
            "iceberg.catalog.type": "rest",
            "iceberg.catalog.uri": self.uri,
            "iceberg.catalog.warehouse": self.warehouse,
            "iceberg.catalog.security": "JWT",
            "iceberg.catalog.oauth2.server-uri": self.oauth2_server_uri,
            "iceberg.catalog.oauth2.scope": "openid",
            CREDENTIAL_PROPERTY: credential,
        }

    def _tls_properties(self) -> dict[str, str]:
        return {
            TLS_CONFIGURER_PROPERTY: self.tls_configurer_impl,
            TLS_CA_FILE_PROPERTY: self.tls_ca_file,
        }

    def _create_statement(self, properties: Mapping[str, str]) -> str:
        return (
            f"CREATE EXTERNAL CATALOG IF NOT EXISTS {self.name}\n"
            "COMMENT 'MIT OL Data Lake, with your own Gravitino access'\n"
            f"PROPERTIES(\n{_properties_sql(properties)}\n);"
        )

    def create_trigger_sql(self) -> str:
        """Render what the CREATE Command's trigger hashes.

        That is the CREATE with the credential replaced by
        ``CREDENTIAL_PLACEHOLDER`` and without the TLS properties. ALTER sets
        both, so changing either must not replace the Command, which would drop
        the catalog and every grant on it.

        :returns: The statement, never executed.
        :rtype: str
        """
        return self._create_statement(self._identity_properties(CREDENTIAL_PLACEHOLDER))

    def create_sql(self, credential: str) -> str:
        """Render the CREATE EXTERNAL CATALOG statement.

        The bootstrap credential has to be here, not only in ``alter_sql``:
        creating the connector initializes ``RESTSessionCatalog``, whose
        ``GET /v1/config`` carries no user session, and Gravitino refuses it
        unauthenticated. With ``security = JWT`` and no credential, StarRocks
        also drops every user's token (StarRocks#75792). The credential needs no
        Gravitino grants, and must hold none: StarRocks commits an INSERT through
        this catalog as the credential, not as the user.

        StarRocks 4.1.7 does not redact this property: SqlCredentialRedactor's
        key set, seeded from PrintableMap.SENSITIVE_KEY, does not include it.
        CREATE and ALTER CATALOG write it in plaintext to fe.audit.log, a
        failed one also to fe.log, and CatalogMgr logs the raw config to fe.log
        on every ALTER that changes something, on each FE. The FE sets no
        LOG_CONSOLE and there is no AuditLoader, so those files stay on the FE
        log volume rather than in Loki. That is tolerable only because the
        credential holds no Gravitino grants. Upstream: StarRocks#82691 (fix
        #82692) for the audit log, #78519 for the CatalogMgr log line.

        :param credential: ``<client_id>:<client_secret>`` of the
            ol-gravitino-catalog Keycloak client, or ``CREDENTIAL_PLACEHOLDER``.

        :returns: The statement.
        :rtype: str
        """
        return self._create_statement(
            {**self._identity_properties(credential), **self._tls_properties()}
        )

    def alter_sql(self, credential: str) -> str:
        """Render the ALTER CATALOG ... SET of the properties that may change.

        ALTER CATALOG swaps the connector in place and keeps the catalog, and
        with it every grant on it, where a changed CREATE would drop it.

        Vended credentials are on (the 4.1.7 default, set to say so). They do
        not fail closed: for a table response with no credential, 4.1.7 falls
        back to the /v1/config credentials and then to the catalog's own S3
        settings (IcebergUtil.getVendedCloudConfiguration), and with none set
        IcebergAwsClientFactory uses the AWS default chain, which is the
        StarRocks pods' IRSA role. No 4.1.7 property refuses that. Gravitino has
        already authorized the loadTable by then, so the fallback reads a table
        the user may read; what it hides is a vending failure. Check vending in
        CloudTrail (``gravitino_irsa_session_<user>`` on the reads), not by
        expecting a failed read. ``aws.s3.region`` only supplies the region when
        the vended credential does not.

        :param credential: As for ``create_sql``.

        :returns: The statement.
        :rtype: str
        """
        properties = {
            CREDENTIAL_PROPERTY: credential,
            "iceberg.catalog.vended-credentials-enabled": "true",
            "aws.s3.region": self.aws_region,
            **self._tls_properties(),
        }
        return f"ALTER CATALOG {self.name} SET (\n{_properties_sql(properties)}\n);"
