"""Tests for the desired state the Gravitino stack hands its grant reconciler.

The mapping in ``lib/data_lake_access.py`` is the policy; these pin the parts of
its translation that would widen access if they regressed: analysts never get a
catalog-wide table privilege, nobody reads ``raw``, the undecided roles hold
nothing, and no role is handed a privilege that manages grants or runs jobs.
"""

import json
from typing import Any

import pytest

from ol_infrastructure.applications.gravitino import reconcile
from ol_infrastructure.applications.gravitino.reconcile import (
    render_desired_state,
    render_role_grants,
)
from ol_infrastructure.lib import data_lake_access
from ol_infrastructure.lib.data_lake_access import (
    CATALOG_WIDE_WRITE_ROLES,
    GOVERNANCE_ROLES,
    layer_database,
)

CATALOG = "ol_data_lake_qa"
TABLE_PRIVILEGES = {"SELECT_TABLE", "MODIFY_TABLE", "CREATE_TABLE"}


def _privileges(securable_object: dict[str, Any]) -> set[str]:
    return {privilege["name"] for privilege in securable_object["privileges"]}


def _by_name(objects: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {
        securable_object["fullName"]: securable_object for securable_object in objects
    }


def test_every_governance_role_is_rendered():
    assert set(render_role_grants("qa", CATALOG)) == set(GOVERNANCE_ROLES)


@pytest.mark.parametrize("role", ["ol_researcher", "ol_instructor"])
def test_undecided_roles_hold_nothing(role):
    assert render_role_grants("qa", CATALOG)[role] == []


@pytest.mark.parametrize("role", ["ol_data_analyst", "ol_business_analyst"])
def test_analyst_roles_are_scoped_to_layer_schemas(role):
    objects = _by_name(render_role_grants("qa", CATALOG)[role])
    assert _privileges(objects.pop(CATALOG)) == {"USE_CATALOG"}
    assert objects
    for full_name, securable_object in objects.items():
        assert securable_object["type"] == "schema"
        assert full_name.startswith(f"{CATALOG}.ol_warehouse_qa_")
        assert _privileges(securable_object) == {"USE_SCHEMA", "SELECT_TABLE"}
    assert f"{CATALOG}.ol_warehouse_qa_raw" not in objects


def test_business_analysts_do_not_read_staging():
    objects = _by_name(render_role_grants("qa", CATALOG)["ol_business_analyst"])
    assert f"{CATALOG}.{layer_database('qa', 'staging')}" not in objects
    assert f"{CATALOG}.{layer_database('qa', 'mart')}" in objects


@pytest.mark.parametrize("role", CATALOG_WIDE_WRITE_ROLES)
def test_engineering_roles_write_the_whole_catalog(role):
    (securable_object,) = render_role_grants("qa", CATALOG)[role]
    assert securable_object["type"] == "catalog"
    assert securable_object["fullName"] == CATALOG
    assert _privileges(securable_object) >= TABLE_PRIVILEGES


def test_only_allow_conditions_and_no_administrative_privileges():
    for objects in render_role_grants("production", "ol_data_lake_production").values():
        for securable_object in objects:
            for privilege in securable_object["privileges"]:
                assert privilege["condition"] == "ALLOW"
                assert not privilege["name"].startswith("MANAGE_")
                assert "JOB" not in privilege["name"]
                assert privilege["name"] != "CREATE_ROLE"


def test_desired_state_is_json_serializable_and_environment_scoped():
    state = render_desired_state(
        env_suffix="production",
        metalake="ol_data_platform",
        catalog="ol_data_lake_production",
        catalog_properties={"catalog-backend": "custom"},
    )
    rendered = json.dumps(state)
    assert "ol_warehouse_qa_" not in rendered
    assert state["owned_schema_prefix"] == "ol_warehouse_production_"
    assert state["catalog"]["provider"] == "lakehouse-iceberg"


def test_retired_role_cannot_also_be_a_governance_role(monkeypatch):
    monkeypatch.setattr(data_lake_access, "RETIRED_ROLES", ("ol_data_analyst",))
    with pytest.raises(ValueError, match="both governance roles and retired"):
        data_lake_access.validate_governance_access()


def test_retired_roles_reach_the_desired_state(monkeypatch):
    monkeypatch.setattr(reconcile, "RETIRED_ROLES", ("ol_auditor",))
    state = render_desired_state("qa", "ol_data_platform", CATALOG, {})
    assert state["retired_roles"] == ["ol_auditor"]
    assert "ol_auditor" not in state["roles"]


def test_unknown_layer_is_rejected():
    with pytest.raises(ValueError, match="not a data lake layer"):
        layer_database("qa", "gold")
