"""Superset and the lake engines grant a role the same single tables.

StarRocks and Gravitino render single-table grants from GOVERNANCE_TABLE_ACCESS.
Superset reads each role's allowed_datasets from ol_governance_roles.json, which
ol-superset roles sync turns into dataset permissions. The two are kept by hand,
so this holds them equal.
"""

import json
from pathlib import Path

import pytest

from ol_infrastructure.lib import data_lake_access
from ol_infrastructure.lib.data_lake_access import (
    GOVERNANCE_TABLE_ACCESS,
    layer_database,
    validate_governance_access,
)

GOVERNANCE_ROLES_JSON = (
    Path(data_lake_access.__file__).parents[1]
    / "applications"
    / "superset"
    / "ol_governance_roles.json"
)


def _superset_allowed_datasets() -> dict[str, set[str]]:
    roles = json.loads(GOVERNANCE_ROLES_JSON.read_text())
    return {
        role["name"]: set(role["allowed_datasets"])
        for role in roles
        if role.get("allowed_datasets")
    }


def test_superset_allowed_datasets_match_table_access() -> None:
    expected = {
        role: {
            f"{layer_database('production', layer)}.{table}"
            for layer, tables in table_layers.items()
            for table in tables
        }
        for role, table_layers in GOVERNANCE_TABLE_ACCESS.items()
    }
    assert _superset_allowed_datasets() == expected


@pytest.mark.parametrize(
    ("table_access", "message"),
    [
        ({"ol_platform_admin": {"mart": ("t",)}}, "not a layer-scoped role"),
        ({"ol_commerce_operations": {"gold": ("t",)}}, "unknown layers"),
        ({"ol_business_analyst": {"mart": ("t",)}}, "reads \\['mart'\\] whole"),
        ({"ol_commerce_operations": {"mart": ()}}, "empty or invalid"),
        ({"ol_commerce_operations": {"mart": ("t; DROP",)}}, "empty or invalid"),
    ],
)
def test_invalid_table_access_is_refused(
    monkeypatch: pytest.MonkeyPatch,
    table_access: dict[str, dict[str, tuple[str, ...]]],
    message: str,
) -> None:
    monkeypatch.setattr(data_lake_access, "GOVERNANCE_TABLE_ACCESS", table_access)
    with pytest.raises(ValueError, match=message):
        validate_governance_access()


@pytest.mark.parametrize(
    ("retired", "message"),
    [
        (
            {"ol_commerce_operations": {"mart": ("marts__combined__orders",)}},
            "both granted and retired",
        ),
        ({"ol_platform_admin": {"mart": ("t",)}}, "not a layer-scoped role"),
        ({"ol_commerce_operations": {"gold": ("t",)}}, "unknown layers"),
        ({"ol_commerce_operations": {"mart": ("t; DROP",)}}, "empty or invalid"),
    ],
)
def test_invalid_retired_tables_are_refused(
    monkeypatch: pytest.MonkeyPatch,
    retired: dict[str, dict[str, tuple[str, ...]]],
    message: str,
) -> None:
    monkeypatch.setattr(data_lake_access, "RETIRED_TABLE_ACCESS", retired)
    with pytest.raises(ValueError, match=message):
        validate_governance_access()


def test_no_role_reaches_the_superset_metadata_db() -> None:
    """Either global grant opens every database in SQL Lab, Superset's own included.

    ol-superset roles sync revokes a global grant a role's definition doesn't
    list, so keeping them out of this file keeps them off the live roles.
    """
    roles = json.loads(GOVERNANCE_ROLES_JSON.read_text())
    global_grants = {"all_database_access", "all_datasource_access"}
    assert {
        role["name"]: listed
        for role in roles
        if (
            listed := global_grants
            & {p["permission"]["name"] for p in role.get("permissions", [])}
        )
    } == {}
    assert [
        role["name"]
        for role in roles
        if "Superset Metadata DB" in role.get("allowed_databases", [])
    ] == []
