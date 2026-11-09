"""The Gravitino catalog's SQL must keep rotation in place and users read-only.

A change to the hashed CREATE replaces its Command, which drops the catalog and
every grant on it, so the credential has to stay out of what is hashed. Writes
through a security=JWT catalog commit as the bootstrap credential, so no role
may be granted more than SELECT.
"""

import re

import pytest

from ol_infrastructure.lib.data_lake_access import (
    GOVERNANCE_LAYER_ACCESS,
    layer_database,
)
from ol_infrastructure.substructure.starrocks.gravitino_catalog import (
    CREDENTIAL_PLACEHOLDER,
    CREDENTIAL_PROPERTY,
    GravitinoCatalog,
    catalog_name,
    governance_grants_sql,
)

CATALOG = GravitinoCatalog(
    name=catalog_name("qa"),
    uri="https://gravitino.gravitino.svc:9433/iceberg",
    warehouse="ol_data_lake_qa",
    oauth2_server_uri="https://sso.example/realms/r/protocol/openid-connect/token",
    aws_region="us-east-1",
)


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


def test_vended_credentials_without_the_irsa_fallback() -> None:
    sql = CATALOG.create_sql("c") + CATALOG.alter_sql("c")

    assert '"iceberg.catalog.vended-credentials-enabled" = "true"' in sql
    assert '"iceberg.catalog.security" = "JWT"' in sql
    assert "use_aws_sdk_default_behavior" not in sql


def test_grants_are_select_only_and_governance_only() -> None:
    sql = governance_grants_sql(catalog_name("qa"), "qa")
    grants = [line for line in sql.splitlines() if line.startswith("GRANT")]

    assert grants
    for grant in grants:
        assert re.match(r"GRANT (USAGE|SELECT) ON ", grant), grant
    roles = {grant.rsplit(" TO ROLE ", 1)[1].rstrip(";") for grant in grants}
    assert roles.isdisjoint({"readonly", "batch_readonly", "app", "admin"})


def test_grants_follow_data_lake_access() -> None:
    sql = governance_grants_sql(catalog_name("qa"), "qa")

    assert (
        "GRANT SELECT ON ALL TABLES IN ALL DATABASES TO ROLE ol_data_engineer;" in sql
    )
    for layer in GOVERNANCE_LAYER_ACCESS["ol_business_analyst"]:
        assert (
            f"GRANT SELECT ON ALL TABLES IN DATABASE {layer_database('qa', layer)}"
            " TO ROLE ol_business_analyst;"
        ) in sql
    assert "ALL DATABASES TO ROLE ol_business_analyst" not in sql
    assert "ol_researcher" not in sql


def test_grants_end_in_default_catalog() -> None:
    sql = governance_grants_sql(catalog_name("qa"), "qa")

    assert sql.splitlines()[-1] == "SET CATALOG default_catalog;"
