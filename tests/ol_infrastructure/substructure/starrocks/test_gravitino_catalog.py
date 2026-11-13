"""The Gravitino catalog's SQL must keep rotation in place.

A change to the hashed CREATE replaces its Command, which drops the catalog and
every grant on it, so the credential and the TLS properties, which ALTER also
sets, have to stay out of what is hashed.
"""

import pytest

from ol_infrastructure.substructure.starrocks.gravitino_catalog import (
    CREDENTIAL_PLACEHOLDER,
    CREDENTIAL_PROPERTY,
    TLS_CA_FILE_PROPERTY,
    TLS_CONFIGURER_PROPERTY,
    GravitinoCatalog,
    catalog_name,
)

CATALOG = GravitinoCatalog(
    name=catalog_name("qa"),
    uri="https://gravitino.gravitino.svc:9433/iceberg",
    warehouse="ol_data_lake_qa",
    oauth2_server_uri="https://sso.example/realms/r/protocol/openid-connect/token",
    aws_region="us-east-1",
    tls_configurer_impl="edu.mit.ol.iceberg.CaFileTLSConfigurer",
    tls_ca_file="/etc/starrocks/gravitino-ca/ca.crt",
)

# What the trigger hashed before the TLS properties existed. If this changes,
# every environment's catalog is dropped and re-created on the next apply.
TRIGGER_SQL_BEFORE_TLS = (
    "CREATE EXTERNAL CATALOG IF NOT EXISTS ol_governed_lake_qa\n"
    "COMMENT 'MIT OL Data Lake, with your own Gravitino access'\n"
    "PROPERTIES(\n"
    '    "type" = "iceberg",\n'
    '    "iceberg.catalog.type" = "rest",\n'
    '    "iceberg.catalog.uri" = "https://gravitino.gravitino.svc:9433/iceberg",\n'
    '    "iceberg.catalog.warehouse" = "ol_data_lake_qa",\n'
    '    "iceberg.catalog.security" = "JWT",\n'
    '    "iceberg.catalog.oauth2.server-uri"'
    ' = "https://sso.example/realms/r/protocol/openid-connect/token",\n'
    '    "iceberg.catalog.oauth2.scope" = "openid",\n'
    '    "iceberg.catalog.oauth2.credential" = "<bootstrap credential>"\n'
    ");"
)


def test_trigger_sql_is_unchanged_by_the_tls_properties() -> None:
    assert CATALOG.create_trigger_sql() == TRIGGER_SQL_BEFORE_TLS


def test_create_and_alter_carry_the_tls_properties() -> None:
    for sql in (CATALOG.create_sql("c"), CATALOG.alter_sql("c")):
        assert (
            f'"{TLS_CONFIGURER_PROPERTY}" = "edu.mit.ol.iceberg.CaFileTLSConfigurer"'
            in sql
        )
        assert f'"{TLS_CA_FILE_PROPERTY}" = "/etc/starrocks/gravitino-ca/ca.crt"' in sql


def test_trigger_sql_does_not_depend_on_tls_or_credential() -> None:
    other = CATALOG.model_copy(
        update={"tls_configurer_impl": "x.Other", "tls_ca_file": "/other.crt"}
    )

    assert other.create_trigger_sql() == CATALOG.create_trigger_sql()
    assert "rest.client.tls" not in CATALOG.create_trigger_sql()


def test_catalog_name_differs_from_the_glue_catalog() -> None:
    assert catalog_name("qa") != "ol_data_lake_qa"


def test_create_and_alter_carry_the_credential() -> None:
    for sql in (CATALOG.create_sql("id:secret"), CATALOG.alter_sql("id:secret")):
        assert f'"{CREDENTIAL_PROPERTY}" = "id:secret"' in sql


def test_placeholder_sql_does_not_depend_on_the_credential() -> None:
    template = CATALOG.create_sql(CREDENTIAL_PLACEHOLDER)

    assert "secret" not in template
    assert template == CATALOG.create_sql("id:rotated").replace(
        "id:rotated", CREDENTIAL_PLACEHOLDER
    )


@pytest.mark.parametrize(
    "credential", ['id:se"cret', "id:se\\cret", "id:sé", "id:\n", ""]
)
def test_credential_starrocks_would_unescape_differently_is_refused(
    credential: str,
) -> None:
    with pytest.raises(ValueError, match="printable ASCII"):
        CATALOG.alter_sql(credential)


def test_vended_credentials_on_and_no_catalog_s3_credentials() -> None:
    sql = CATALOG.create_sql("c") + CATALOG.alter_sql("c")

    assert '"iceberg.catalog.vended-credentials-enabled" = "true"' in sql
    assert '"iceberg.catalog.security" = "JWT"' in sql
    assert "use_aws_sdk_default_behavior" not in sql
