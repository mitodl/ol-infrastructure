"""The StarRocks lineage job must prune only the lake edges it wrote and no longer uses.

The job adds a DbtLineage edge from each lake table a StarRocks model reads.  When
a model stops reading a table, or leaves the project while its table stays in OM,
the edge has to go.  Edges from query-log lineage or manual edits, and the dbt
edges between StarRocks tables that OM's own dbt workflow writes, must stay, and
a manifest that looks like every model vanished must not empty the graph.
"""

import ast
import importlib
import importlib.util
import sys
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

MODULE = "ol_infrastructure.substructure.open_metadata.scripts.starrocks_dbt_lineage"
LAKE = "Starburst Galaxy"
STARROCKS = "StarRocks"
STARROCKS_DB = "default_catalog"
SCHEMAS = frozenset({"b2b_analytics"})


def _metadata_modules(script: Path) -> set[str]:
    """Return every ``metadata`` package path the script imports, with parents."""
    modules: set[str] = set()
    for node in ast.walk(ast.parse(script.read_text())):
        if isinstance(node, ast.ImportFrom) and node.module:
            parts = node.module.split(".")
            if parts[0] == "metadata":
                modules.update(".".join(parts[:i]) for i in range(1, len(parts) + 1))
    return modules


@pytest.fixture(scope="module")
def job() -> Iterator[ModuleType]:
    """Import the job with the OM ingestion SDK stubbed out.

    The script runs in the ingestion image; this project does not install the
    ``metadata`` package it imports.
    """
    spec = importlib.util.find_spec(MODULE)
    assert spec is not None
    assert spec.origin is not None
    with pytest.MonkeyPatch.context() as patch:
        for name in _metadata_modules(Path(spec.origin)):
            patch.setitem(sys.modules, name, MagicMock())
        yield importlib.import_module(MODULE)
        sys.modules.pop(MODULE)


def _quote(name: str) -> str:
    return f'"{name}"' if "." in name else name


def _fqn(*parts: str) -> str:
    return ".".join(_quote(part) for part in parts)


def _manifest(
    models: dict[str, list[str]], schema: str = "b2b_analytics"
) -> dict[str, Any]:
    """Build a manifest whose models read the named lake tables.

    :param models: Model name to ``"database.schema.table"`` of each source.
    """
    sources = {
        f"source.ol.{table}": {
            "database": table.split(".")[0],
            "schema": table.split(".")[1],
            "name": table.split(".")[2],
            "identifier": None,
        }
        for tables in models.values()
        for table in tables
    }
    nodes = {
        f"model.ol.{name}": {
            "resource_type": "model",
            "schema": schema,
            "name": name,
            "alias": None,
            "depends_on": {"nodes": [f"source.ol.{table}" for table in tables]},
        }
        for name, tables in models.items()
    }
    return {"nodes": nodes, "sources": sources}


def _ref(node_id: str, fqn: str) -> dict[str, str]:
    return {"id": node_id, "type": "table", "fullyQualifiedName": fqn}


def _lineage(
    table: dict[str, str], upstream: list[tuple[dict[str, str], str | None]]
) -> dict[str, Any]:
    """Build a lineage API response for a table and its upstream nodes.

    :param upstream: Each upstream node and its edge's source, or None for an
        edge stored without lineageDetails.
    """
    return {
        "entity": table,
        "nodes": [node for node, _ in upstream],
        "upstreamEdges": [
            {
                "fromEntity": node["id"],
                "toEntity": table["id"],
                **({"lineageDetails": {"source": source}} if source else {}),
            }
            for node, source in upstream
        ],
        "downstreamEdges": [],
    }


def _lake(table: str) -> dict[str, str]:
    return _ref(f"lake-{table}", _fqn(LAKE, *table.split(".")))


def _starrocks(name: str, schema: str = "b2b_analytics") -> dict[str, str]:
    return _ref(f"sr-{name}", _fqn(STARROCKS, STARROCKS_DB, schema, name))


def _expected(
    job: ModuleType, manifest: dict[str, Any]
) -> dict[tuple[str, str], set[str]]:
    return job.lake_sources_by_model(
        manifest, SCHEMAS, lambda db, schema, table: _fqn(LAKE, db, schema, table)
    )


LAKE_PREFIX = f"{LAKE}.".casefold()


def test_expected_sources_cover_every_model_in_the_schemas(job: ModuleType) -> None:
    """Every model in the schemas gets an entry, keyed and valued without case."""
    manifest = _manifest(
        {
            "orders": ["ol_warehouse.marts.Orders", "ol_warehouse.marts.users"],
            "summary": [],
        }
    )
    other = _manifest({"elsewhere": ["ol_warehouse.marts.x"]}, schema="other")
    manifest["nodes"].update(other["nodes"])
    manifest["sources"].update(other["sources"])

    assert _expected(job, manifest) == {
        ("b2b_analytics", "orders"): {
            "starburst galaxy.ol_warehouse.marts.orders",
            "starburst galaxy.ol_warehouse.marts.users",
        },
        # A model with no lake source is still in the project.
        ("b2b_analytics", "summary"): set(),
    }


def test_alias_and_identifier_name_the_tables(job: ModuleType) -> None:
    """The model's alias and the source's identifier name the physical tables."""
    manifest = _manifest({"orders": ["ol_warehouse.marts.orders_src"]})
    manifest["nodes"]["model.ol.orders"]["alias"] = "Orders_Alias"
    manifest["sources"]["source.ol.ol_warehouse.marts.orders_src"]["identifier"] = (
        "orders_physical"
    )

    assert _expected(job, manifest) == {
        ("b2b_analytics", "orders_alias"): {
            "starburst galaxy.ol_warehouse.marts.orders_physical"
        }
    }


def test_only_dbt_edges_from_the_lake_are_owned(job: ModuleType) -> None:
    """Query-log, manual and StarRocks-internal dbt edges are not the job's."""
    orders = _starrocks("orders")
    lineage = _lineage(
        orders,
        [
            (_lake("ol_warehouse.marts.orders"), "DbtLineage"),
            (_lake("ol_warehouse.marts.queried"), "QueryLineage"),
            (_lake("ol_warehouse.marts.manual"), "Manual"),
            (_lake("ol_warehouse.marts.no_details"), None),
            # OM's dbt workflow writes this one between StarRocks tables.
            (_starrocks("stg_orders"), "DbtLineage"),
        ],
    )

    assert job.lake_dbt_edges(lineage, LAKE_PREFIX) == [
        job.LakeEdge(
            from_type="table",
            from_id="lake-ol_warehouse.marts.orders",
            from_fqn="Starburst Galaxy.ol_warehouse.marts.orders",
            to_id="sr-orders",
            to_fqn="StarRocks.default_catalog.b2b_analytics.orders",
        )
    ]


def test_a_service_sharing_the_lake_name_prefix_is_not_the_lake(
    job: ModuleType,
) -> None:
    """The prefix ends at the separator, so a longer service name fails it."""
    lineage = _lineage(
        _starrocks("orders"),
        [(_ref("other", "Starburst Galaxy QA.db.schema.orders"), "DbtLineage")],
    )

    assert job.lake_dbt_edges(lineage, LAKE_PREFIX) == []


def test_edges_the_manifest_still_declares_are_kept(job: ModuleType) -> None:
    """Only the lake table the model stopped reading is stale."""
    manifest = _manifest({"orders": ["ol_warehouse.marts.orders"]})
    edges = job.lake_dbt_edges(
        _lineage(
            _starrocks("orders"),
            [
                (_lake("ol_warehouse.marts.orders"), "DbtLineage"),
                (_lake("ol_warehouse.marts.dropped"), "DbtLineage"),
            ],
        ),
        LAKE_PREFIX,
    )

    stale = job.stale_edges(edges, _expected(job, manifest)["b2b_analytics", "orders"])

    assert [edge.from_fqn for edge in stale] == [
        "Starburst Galaxy.ol_warehouse.marts.dropped"
    ]


def test_a_removed_model_loses_every_lake_edge(job: ModuleType) -> None:
    """A table that is no longer a model keeps none of the job's edges."""
    edges = job.lake_dbt_edges(
        _lineage(
            _starrocks("retired"),
            [(_lake("ol_warehouse.marts.orders"), "DbtLineage")],
        ),
        LAKE_PREFIX,
    )

    assert job.stale_edges(edges, set()) == edges


@pytest.mark.parametrize(
    ("model_count", "stale", "current"),
    [
        (12, 0, 40),
        (12, 3, 40),
        # A small graph may lose a few edges even past the fraction.
        (12, 5, 6),
        (12, 20, 40),
    ],
)
def test_prune_allowed(
    job: ModuleType, model_count: int, stale: int, current: int
) -> None:
    """Routine model changes stay under the limit."""
    assert job.prune_refusal(model_count, stale, current, 0.5) is None


@pytest.mark.parametrize(
    ("model_count", "stale", "current"),
    [
        (0, 0, 0),
        (0, 3, 40),
        (12, 21, 40),
        (12, 40, 40),
    ],
)
def test_prune_refused(
    job: ModuleType, model_count: int, stale: int, current: int
) -> None:
    """No StarRocks models, or more than half the edges going, stops the prune."""
    assert job.prune_refusal(model_count, stale, current, 0.5) is not None


class _FakeAPIError(Exception):
    def __init__(self, path: str, status_code: int) -> None:
        super().__init__(path)
        self.status_code = status_code


class _FakeClient:
    """Deletes edges; a path in ``gone`` is a 404 and one in ``rejected`` a 500."""

    def __init__(self, gone: set[str], rejected: frozenset[str] = frozenset()) -> None:
        self.gone = gone
        self.rejected = rejected
        self.deleted: list[str] = []

    def delete(self, path: str) -> None:
        if path in self.gone:
            raise _FakeAPIError(path, 404)
        if path in self.rejected:
            raise _FakeAPIError(path, 500)
        self.deleted.append(path)


class _FakeOM:
    """Serves the StarRocks tables of one schema and their lineage."""

    def __init__(self, lineages: list[dict[str, Any]], gone: set[str]) -> None:
        self.lineages = {lineage["entity"]["id"]: lineage for lineage in lineages}
        self.client = _FakeClient(gone)

    def list_all_entities(self, entity: object, params: dict[str, str]) -> list[Any]:  # noqa: ARG002
        assert params == {
            "databaseSchema": _fqn(STARROCKS, STARROCKS_DB, "b2b_analytics")
        }
        return [
            SimpleNamespace(
                id=SimpleNamespace(root=lineage["entity"]["id"]),
                name=SimpleNamespace(
                    root=lineage["entity"]["fullyQualifiedName"].split(".")[-1]
                ),
            )
            for lineage in self.lineages.values()
        ]

    def get_lineage_by_id(
        self,
        entity: object,  # noqa: ARG002
        entity_id: str,
        up_depth: int,
        down_depth: int,
    ) -> dict[str, Any]:
        assert (up_depth, down_depth) == (1, 0)
        return self.lineages[entity_id]


def _fake_build(_metadata: object, **kwargs: Any) -> str:
    """Build an FQN the way ``metadata.utils.fqn.build`` does for these entities."""
    parts = ("service_name", "database_name", "schema_name", "table_name")
    return _fqn(*(kwargs[part] for part in parts if part in kwargs))


@pytest.fixture
def fake_sdk(job: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace the stubbed FQN helpers and API error with working fakes."""
    monkeypatch.setattr(
        job, "fqn", SimpleNamespace(build=_fake_build, quote_name=_quote)
    )
    monkeypatch.setattr(job, "APIError", _FakeAPIError)


def _prune(
    job: ModuleType, om: _FakeOM, manifest: dict[str, Any], settings: Any
) -> tuple[int, int]:
    return job._apply_prune(om, job._plan_prune(om, manifest, settings), settings)


def _settings(job: ModuleType, *, dry_run: bool) -> Any:
    return job.Settings(
        starrocks_service=STARROCKS,
        starrocks_database=STARROCKS_DB,
        lake_service=LAKE,
        schemas=SCHEMAS,
        dry_run=dry_run,
        max_fraction=0.5,
    )


def _graph() -> list[dict[str, Any]]:
    return [
        _lineage(
            _starrocks("orders"),
            [
                (_lake("ol_warehouse.marts.orders"), "DbtLineage"),
                (_lake("ol_warehouse.marts.dropped"), "DbtLineage"),
                (_lake("ol_warehouse.marts.queried"), "QueryLineage"),
                (_starrocks("stg_orders"), "DbtLineage"),
            ],
        ),
        _lineage(
            _starrocks("retired"), [(_lake("ol_warehouse.marts.users"), "DbtLineage")]
        ),
        _lineage(
            _starrocks("users"), [(_lake("ol_warehouse.marts.users"), "DbtLineage")]
        ),
    ]


MANIFEST = _manifest(
    {
        "orders": ["ol_warehouse.marts.orders"],
        "users": ["ol_warehouse.marts.users"],
    }
)


@pytest.mark.usefixtures("fake_sdk")
def test_prune_deletes_only_stale_lake_edges(
    job: ModuleType, caplog: pytest.LogCaptureFixture
) -> None:
    """Applying deletes the stale edges, by id, and logs each one."""
    om = _FakeOM(_graph(), gone=set())

    with caplog.at_level("INFO"):
        assert _prune(job, om, MANIFEST, _settings(job, dry_run=False)) == (2, 0)

    assert om.client.deleted == [
        "/lineage/table/lake-ol_warehouse.marts.dropped/table/sr-orders",
        "/lineage/table/lake-ol_warehouse.marts.users/table/sr-retired",
    ]
    assert (
        "Deleting stale lake edge Starburst Galaxy.ol_warehouse.marts.dropped"
        " -> StarRocks.default_catalog.b2b_analytics.orders" in caplog.messages
    )


@pytest.mark.usefixtures("fake_sdk")
def test_dry_run_logs_without_deleting(
    job: ModuleType, caplog: pytest.LogCaptureFixture
) -> None:
    """A dry run logs the edges it would delete and deletes none."""
    om = _FakeOM(_graph(), gone=set())

    with caplog.at_level("INFO"):
        assert _prune(job, om, MANIFEST, _settings(job, dry_run=True)) == (2, 0)

    assert om.client.deleted == []
    assert sum(message.startswith("Would delete") for message in caplog.messages) == 2


@pytest.mark.usefixtures("fake_sdk")
def test_an_edge_already_gone_is_not_a_failure(job: ModuleType) -> None:
    """A 404 on delete means the edge was removed some other way first."""
    om = _FakeOM(
        _graph(),
        gone={"/lineage/table/lake-ol_warehouse.marts.users/table/sr-retired"},
    )

    assert _prune(job, om, MANIFEST, _settings(job, dry_run=False)) == (1, 0)


@pytest.mark.usefixtures("fake_sdk")
def test_a_manifest_without_starrocks_models_prunes_nothing(job: ModuleType) -> None:
    """A manifest from the wrong target must not look like every model left."""
    om = _FakeOM(_graph(), gone=set())
    elsewhere = _manifest({"orders": ["ol_warehouse.marts.orders"]}, schema="other")

    assert _prune(job, om, elsewhere, _settings(job, dry_run=False)) == (0, 1)
    assert om.client.deleted == []


@pytest.mark.usefixtures("fake_sdk")
def test_a_prune_past_the_limit_deletes_nothing(job: ModuleType) -> None:
    """Losing most of the lake edges in one run is refused and fails the job."""
    lineages = [
        _lineage(
            _starrocks(f"model_{i}"),
            [(_lake(f"ol_warehouse.marts.t{i}"), "DbtLineage")],
        )
        for i in range(10)
    ]
    om = _FakeOM(lineages, gone=set())
    # Only one model left reads the lake: nine of ten edges look stale.
    manifest = _manifest({"model_0": ["ol_warehouse.marts.t0"]})

    assert _prune(job, om, manifest, _settings(job, dry_run=False)) == (0, 1)
    assert om.client.deleted == []


@pytest.mark.usefixtures("fake_sdk")
def test_a_rejected_delete_is_a_failure(job: ModuleType) -> None:
    """Any API error other than a 404 fails the job."""
    om = _FakeOM(_graph(), gone=set())
    om.client.rejected = frozenset(
        {"/lineage/table/lake-ol_warehouse.marts.users/table/sr-retired"}
    )

    assert _prune(job, om, MANIFEST, _settings(job, dry_run=False)) == (1, 1)
    assert om.client.deleted == [
        "/lineage/table/lake-ol_warehouse.marts.dropped/table/sr-orders"
    ]


@pytest.mark.usefixtures("fake_sdk")
def test_edges_a_bad_manifest_links_do_not_dilute_the_guard(
    job: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A manifest naming the wrong lake tables must not delete the right edges.

    Its link step adds one edge per model.  Counted in the total, those edges
    would make the correct ones only half of it, under the limit.
    """
    lineages = [
        _lineage(
            _starrocks(f"model_{i}"),
            [(_lake(f"ol_warehouse.marts.t{i}"), "DbtLineage")],
        )
        for i in range(10)
    ]
    om = _FakeOM(lineages, gone=set())
    wrong = _manifest({f"model_{i}": [f"ol_warehouse.wrong.t{i}"] for i in range(10)})

    def link(
        metadata: _FakeOM, manifest: dict[str, Any], settings: Any
    ) -> tuple[int, int, set[str]]:
        for source, _, model in job.source_edges(manifest, settings.schemas):
            node = _lake(f"{source['database']}.{source['schema']}.{source['name']}")
            lineage = metadata.lineages[f"sr-{model}"]
            lineage["nodes"].append(node)
            lineage["upstreamEdges"].append(
                {
                    "fromEntity": node["id"],
                    "toEntity": lineage["entity"]["id"],
                    "lineageDetails": {"source": "DbtLineage"},
                }
            )
        return 10, 0, set()

    monkeypatch.setattr(job, "_link", link)

    counts = job._sync(om, wrong, _settings(job, dry_run=False))

    assert (counts.linked, counts.pruned, counts.prune_failed) == (10, 0, 1)
    assert om.client.deleted == []
