"""The domains job must give dbt's rules to the tables dbt leaves out, and Superset
the domain of the tables it reads.

Tables dbt builds or declares stay with the dbt job.  A dataset or dashboard
reading more than one domain gets none, and nothing a run cannot resolve fails it.
"""

from collections.abc import Iterator
from typing import Any
from unittest.mock import MagicMock

import pytest
import requests

from ol_infrastructure.substructure.open_metadata.scripts import (
    domain_assignment as job,
)

LAKE = "Starburst Galaxy.ol_data_lake_production"
GLUE = "Glue.610119931565"
RAW = "ol_warehouse_production_raw"
DIMENSIONAL = "ol_warehouse_production_dimensional"
SCHEMA_INCLUDES = [r"ol_warehouse_[a-z]+_(dimensional|raw)$"]

RULES = job.DomainRules.from_artifact(
    {
        "domains": ["commerce", "learners-identity"],
        "rules": [
            {"domain": None, "match": ["*django_migrations"]},
            {"domain": "commerce", "match": ["*ORDER*"]},
            {"domain": "learners-identity", "match": ["*user*"]},
        ],
    }
)


def _ref(name: str, *, inherited: bool = False) -> dict[str, Any]:
    return {"id": f"id-{name}", "type": "domain", "name": name, "inherited": inherited}


def _table(
    schema: str, name: str, *domains: dict[str, Any], prefix: str = LAKE
) -> dict[str, Any]:
    return {
        "id": f"{schema}.{name}",
        "name": name,
        "fullyQualifiedName": f"{prefix}.{schema}.{name}",
        "databaseSchema": {"name": schema},
        "domains": list(domains),
    }


def _asset(fqn: str, *domains: dict[str, Any]) -> dict[str, Any]:
    return {"id": fqn, "fullyQualifiedName": fqn, "domains": list(domains)}


def _lineage(entity_id: str, *upstream: tuple[str, str]) -> dict[str, Any]:
    """Build a one-hop lineage response.

    :param upstream: ``(type, fqn)`` of each node with an edge into the entity.
    """
    return {
        "entity": {"id": entity_id},
        "nodes": [
            {"id": fqn, "type": entity_type, "fullyQualifiedName": fqn}
            for entity_type, fqn in upstream
        ],
        "upstreamEdges": [
            {"fromEntity": fqn, "toEntity": entity_id} for _, fqn in upstream
        ],
    }


def _plan_tables(*tables: dict[str, Any], dbt_owned=frozenset()):
    report = job.Report()
    changes, domains = job.plan_tables(
        tables, set(dbt_owned), RULES, SCHEMA_INCLUDES, report
    )
    return changes, domains, report


def test_first_matching_rule_wins_ignoring_case():
    assert RULES.match("raw__mitxonline__app__postgres__users_user_order") == (
        True,
        "commerce",
    )
    assert RULES.match("raw__app__django_migrations") == (True, None)
    assert RULES.match("raw__app__wagtail_page") == (False, None)


def test_dbt_relations_use_the_alias_and_identifier():
    manifest = {
        "nodes": {
            "model.ol.dim_user": {
                "resource_type": "model",
                "schema": DIMENSIONAL,
                "name": "dim_user",
                "alias": "Dim_User_V2",
            },
            "test.ol.not_null": {
                "resource_type": "test",
                "schema": DIMENSIONAL,
                "name": "not_null",
                "alias": None,
            },
        },
        "sources": {
            "source.ol.raw.orders": {
                "schema": RAW,
                "name": "orders",
                "identifier": "raw__app__orders",
            }
        },
    }

    assert job.dbt_relations(manifest) == {
        (DIMENSIONAL, "dim_user_v2"),
        (RAW, "raw__app__orders"),
    }


def test_an_inherited_domain_is_not_the_assets_own():
    assert job.own_domain(_asset("a", _ref("commerce", inherited=True))) is None
    assert job.own_domain(_asset("a", _ref("commerce"))) == "commerce"


def test_split_fqn_keeps_a_quoted_name_whole():
    assert job.fqn_relation_key(f'{GLUE}.{RAW}."raw.app.Orders"') == (
        RAW,
        "raw.app.orders",
    )


def test_undeclared_tables_take_the_rule_domain_and_dbt_tables_are_left_alone():
    changes, domains, report = _plan_tables(
        _table(RAW, "raw__app__order"),
        # dbt knows it and set no domain; the rule would say learners-identity.
        _table(RAW, "raw__app__user"),
        _table(DIMENSIONAL, "dim_user", _ref("learners-identity")),
        _table(RAW, "raw__app__django_migrations"),
        _table(RAW, "raw__app__wagtail_page"),
        _table("information_schema", "users"),
        dbt_owned={(RAW, "raw__app__user"), (DIMENSIONAL, "dim_user")},
    )

    assert [(c.label, c.current, c.wanted) for c in changes] == [
        (f"{LAKE}.{RAW}.raw__app__order", None, "commerce")
    ]
    assert report.outcomes == {
        "left to the dbt job": 2,
        "in no domain by rule": 1,
        "no rule matches": 1,
    }
    assert report.unassigned == {
        "no rule matches": [f"{LAKE}.{RAW}.raw__app__wagtail_page"]
    }
    assert domains == {
        (RAW, "raw__app__order"): "commerce",
        (RAW, "raw__app__user"): None,
        (DIMENSIONAL, "dim_user"): "learners-identity",
        (RAW, "raw__app__django_migrations"): None,
        (RAW, "raw__app__wagtail_page"): None,
        ("information_schema", "users"): None,
    }


def test_a_rule_domain_replaces_a_different_one_and_keeps_a_matching_one():
    changes, _, report = _plan_tables(
        _table(RAW, "raw__app__order", _ref("learners-identity")),
        _table(RAW, "raw__app__order_line", _ref("commerce")),
        _table(RAW, "raw__app__order_tax", _ref("learners-identity", inherited=True)),
    )

    assert [(c.label, c.current, c.wanted) for c in changes] == [
        (f"{LAKE}.{RAW}.raw__app__order", "learners-identity", "commerce"),
        (f"{LAKE}.{RAW}.raw__app__order_tax", None, "commerce"),
    ]
    assert report.outcomes == {"unchanged": 1}


TABLE_DOMAINS = {
    (DIMENSIONAL, "dim_user"): "learners-identity",
    (DIMENSIONAL, "dim_date"): None,
    (RAW, "raw__app__order"): "commerce",
}


def _plan_datasets(*datasets: tuple[dict[str, Any], dict[str, Any]]):
    report = job.Report()
    changes, reads = job.plan_lineage_assets(
        "dashboard/datamodels", datasets, TABLE_DOMAINS, {}, report
    )
    return changes, reads, report


def test_a_dataset_over_a_glue_table_takes_its_trino_twins_domain():
    dataset = _asset("Superset.model.84")
    changes, reads, report = _plan_datasets(
        (
            dataset,
            _lineage(
                dataset["id"],
                ("table", f"{GLUE}.{DIMENSIONAL}.dim_user"),
                ("table", f"{LAKE}.{DIMENSIONAL}.dim_date"),
            ),
        )
    )

    assert [(c.collection, c.label, c.wanted) for c in changes] == [
        ("dashboard/datamodels", "Superset.model.84", "learners-identity")
    ]
    assert reads == {"Superset.model.84": {"learners-identity"}}
    assert report.outcomes == {}


def test_a_dataset_spanning_domains_gets_none_and_keeps_what_it_has():
    spanning = _asset("Superset.model.168", _ref("commerce"))
    orphan = _asset("Superset.model.113", _ref("commerce"))
    changes, reads, report = _plan_datasets(
        (
            spanning,
            _lineage(
                spanning["id"],
                ("table", f"{LAKE}.{DIMENSIONAL}.dim_user"),
                ("table", f"{LAKE}.{RAW}.raw__app__order"),
            ),
        ),
        (orphan, _lineage(orphan["id"])),
    )

    assert changes == []
    assert report.unassigned == {
        "spans several domains": [
            "Superset.model.168 ['commerce', 'learners-identity']"
        ],
        "reads no table with a domain": ["Superset.model.113"],
    }
    assert reads == {
        "Superset.model.168": {"commerce", "learners-identity"},
        "Superset.model.113": {"commerce"},
    }


def test_a_dashboard_takes_the_one_domain_its_datasets_read():
    one = _asset("Superset.21")
    two = _asset("Superset.45")
    dataset_domains = {
        "Superset.model.41": {"commerce"},
        "Superset.model.44": {"commerce"},
        "Superset.model.84": {"learners-identity"},
    }
    report = job.Report()
    changes, _ = job.plan_lineage_assets(
        "dashboards",
        [
            (
                one,
                _lineage(
                    one["id"],
                    ("dashboardDataModel", "Superset.model.41"),
                    ("dashboardDataModel", "Superset.model.44"),
                ),
            ),
            (
                two,
                _lineage(
                    two["id"],
                    ("dashboardDataModel", "Superset.model.41"),
                    ("dashboardDataModel", "Superset.model.84"),
                ),
            ),
        ],
        TABLE_DOMAINS,
        dataset_domains,
        report,
    )

    assert [(c.label, c.wanted) for c in changes] == [("Superset.21", "commerce")]
    assert report.unassigned == {
        "spans several domains": ["Superset.45 ['commerce', 'learners-identity']"]
    }


CHANGE = job.Change(
    collection="dashboards",
    entity_id="dash-1",
    label="Superset.21",
    current="learners-identity",
    wanted="commerce",
)


def test_a_dry_run_changes_nothing():
    client = MagicMock()
    report = job.Report()

    job.apply_changes(client, [CHANGE], {"commerce": "c-1"}, report, dry_run=True)

    client.set_domain.assert_not_called()
    assert report.outcomes == {"set (dry run)": 1}


def test_a_domain_missing_in_openmetadata_is_reported_not_set():
    client = MagicMock()
    report = job.Report()

    job.apply_changes(client, [CHANGE], {}, report, dry_run=False)

    client.set_domain.assert_not_called()
    assert report.unassigned == {
        "domain commerce missing in OpenMetadata": ["Superset.21"]
    }


def test_a_rejected_patch_is_counted_and_the_rest_still_run():
    client = MagicMock()
    client.set_domain.side_effect = [requests.HTTPError("403"), None]
    report = job.Report()

    job.apply_changes(
        client, [CHANGE, CHANGE], {"commerce": "c-1"}, report, dry_run=False
    )

    assert report.outcomes == {"failed": 1, "set": 1}
    client.set_domain.assert_called_with("dashboards", "dash-1", "c-1")


def test_set_domain_replaces_the_domains_with_a_json_patch(
    monkeypatch: pytest.MonkeyPatch,
):
    client = job.OpenMetadataClient("http://openmetadata:8585/api/", "token")
    patch = MagicMock()
    monkeypatch.setattr(client._session, "patch", patch)

    client.set_domain("tables", "t-1", "c-1")

    patch.assert_called_once()
    args, kwargs = patch.call_args
    assert args == ("http://openmetadata:8585/api/tables/t-1",)
    assert kwargs["headers"] == {"Content-Type": "application/json-patch+json"}
    assert kwargs["json"] == [
        {"op": "add", "path": "/domains", "value": [{"id": "c-1", "type": "domain"}]}
    ]


def test_list_all_follows_the_paging_cursor(monkeypatch: pytest.MonkeyPatch):
    client = job.OpenMetadataClient("http://openmetadata:8585/api", "token")
    pages: Iterator[dict[str, Any]] = iter(
        [
            {"data": [{"name": "a"}], "paging": {"after": "cursor"}},
            {"data": [{"name": "b"}], "paging": {}},
        ]
    )
    calls: list[dict[str, Any]] = []

    def _get(_path: str, params: dict[str, Any]) -> dict[str, Any]:
        calls.append(params)
        return next(pages)

    monkeypatch.setattr(client, "_get", _get)

    assert [e["name"] for e in client.list_all("domains", {})] == ["a", "b"]
    assert calls[1]["after"] == "cursor"
