"""Tests for bin/omnigraph-cutover-pr's refusals, rewrite and PR body.

The rewrite fixture is the tail of Production's stack config on either side of
the hand-made fmt9 cutover commit (86b814fd5), so the script is checked
against a cutover a person actually made and reviewed.
"""

import copy
import importlib.util
import sys
from importlib.machinery import SourceFileLoader
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest


@pytest.fixture
def script(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """Load the extensionless script without invoking its CLI."""
    path = Path(__file__).resolve().parents[1] / "bin" / "omnigraph-cutover-pr"
    loader = SourceFileLoader("omnigraph_cutover_pr", str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, loader.name, module)
    loader.exec_module(module)
    return module


OLD_IMAGE = (
    "610119931565.dkr.ecr.us-east-1.amazonaws.com/omnigraph-server"
    "@sha256:a55401eef65d538b354850ea756de5f6064875f89d163f95567d255d155729b5"
)

PRODUCTION_ARMED = f"""\
  omnigraph:keycloak_url: https://sso.ol.mit.edu
  aws:region: us-east-1
  vault:address: https://vault-production.odl.mit.edu
  vault_server:env_namespace: operations.production
  omnigraph:storage_prefix: fmt6
  # Cross-checked against storage_prefix at preview time — see the CI
  # config's comment on this same key for why.
  omnigraph:internal_schema_version: 6
  omnigraph:migrate_from_image: {OLD_IMAGE}
  omnigraph:migrate_to_prefix: fmt9
secretsprovider: awskms://alias/infrastructure-secrets-production
"""

PRODUCTION_CUT_OVER = """\
  omnigraph:keycloak_url: https://sso.ol.mit.edu
  aws:region: us-east-1
  vault:address: https://vault-production.odl.mit.edu
  vault_server:env_namespace: operations.production
  omnigraph:storage_prefix: fmt9
  # Cross-checked against storage_prefix at preview time — see the CI
  # config's comment on this same key for why.
  omnigraph:internal_schema_version: 9
secretsprovider: awskms://alias/infrastructure-secrets-production
"""


def _graph(before: dict[str, int], collapsed: dict[str, int]) -> dict[str, Any]:
    after = {t: n - collapsed.get(t, 0) for t, n in before.items()}
    return {
        "ok": True,
        "before": before,
        "collapsed_duplicates": collapsed,
        "expected": after,
        "after": after,
        "new_tables": [],
        "missing_tables": [],
        "changed_tables": [],
    }


VERDICT: dict[str, Any] = {
    "ok": True,
    "old_root": "s3://ol-data-witan-production/fmt6",
    "new_root": "s3://ol-data-witan-production/fmt9",
    "graphs": {
        "council": _graph(
            {"Memory": 900, "ParentOf": 40, "Tagged": 700},
            {"ParentOf": 1, "Tagged": 59},
        ),
        "code-agent-kit": _graph({"Symbol": 5000, "Calls": 12000}, {}),
    },
    "old_internal_schema": {"council": 6, "code-agent-kit": 6},
    "new_internal_schema": {"council": 9, "code-agent-kit": 9},
    "format_problems": [],
    "status": "finished",
    "binaries": {"old": "omnigraph 0.10.0", "new": "omnigraph 0.11.0"},
    "finished_at": "2026-09-16T18:51:00+00:00",
}


def _verdict(**overrides: Any) -> dict[str, Any]:
    return copy.deepcopy(VERDICT) | overrides


def test_the_rewrite_reproduces_the_production_fmt9_cutover(
    script: ModuleType,
) -> None:
    """Same four lines a person changed by hand, and nothing else."""
    cutover = script.plan_cutover(VERDICT, PRODUCTION_ARMED, "Production")
    rewritten = script.rewrite_config(PRODUCTION_ARMED, cutover)

    assert rewritten == PRODUCTION_CUT_OVER
    assert script.unexpected_changes(PRODUCTION_ARMED, rewritten, cutover) == []
    assert cutover.branch == "omnigraph-cutover-production-fmt9"
    assert cutover.title == ("feat(omnigraph): cut Production over to storage format 9")


def test_a_quoted_schema_version_is_read_and_rewritten(script: ModuleType) -> None:
    """`pulumi config set` wrote `'6'` where a hand edit writes `6`."""
    armed = PRODUCTION_ARMED.replace(
        "internal_schema_version: 6", "internal_schema_version: '6'"
    )
    cutover = script.plan_cutover(VERDICT, armed, "Production")

    assert script.rewrite_config(armed, cutover) == PRODUCTION_CUT_OVER


@pytest.mark.parametrize(
    ("verdict", "config", "env", "reason"),
    [
        pytest.param(
            {
                "ok": False,
                "status": "in_progress",
                "old_root": VERDICT["old_root"],
                "new_root": VERDICT["new_root"],
                "started_at": "2026-10-01T00:00:00+00:00",
            },
            PRODUCTION_ARMED,
            "Production",
            "never reached verification",
            id="run-never-finished",
        ),
        pytest.param(
            _verdict(ok=False, format_problems=["not all on one format"]),
            PRODUCTION_ARMED,
            "Production",
            "not ok",
            id="failed-verdict",
        ),
        pytest.param(
            _verdict(
                old_root="s3://ol-data-witan-ci/fmt6",
                new_root="s3://ol-data-witan-ci/fmt9",
            ),
            PRODUCTION_ARMED,
            "Production",
            "not in Production's bucket",
            id="another-envs-verdict",
        ),
        pytest.param(
            _verdict(new_internal_schema={"council": 9, "code-agent-kit": 8}),
            PRODUCTION_ARMED,
            "Production",
            "not on one format",
            id="mixed-new-format",
        ),
        pytest.param(
            _verdict(new_root="s3://ol-data-witan-production/fmt10"),
            PRODUCTION_ARMED,
            "Production",
            "does not name format 9",
            id="prefix-disagrees-with-format",
        ),
        pytest.param(
            VERDICT,
            PRODUCTION_ARMED.replace("storage_prefix: fmt6", "storage_prefix: fmt5"),
            "Production",
            "serves storage_prefix fmt5",
            id="rebuilt-from-a-root-not-served",
        ),
        pytest.param(
            VERDICT,
            PRODUCTION_ARMED.replace(
                "migrate_to_prefix: fmt9", "migrate_to_prefix: fmt10"
            ),
            "Production",
            "targets fmt10",
            id="a-different-migration-armed",
        ),
        pytest.param(
            VERDICT,
            PRODUCTION_CUT_OVER,
            "Production",
            "serves storage_prefix fmt9",
            id="already-cut-over",
        ),
        pytest.param(
            VERDICT,
            PRODUCTION_ARMED + "  omnigraph:migrate_to_prefix: fmt9\n",
            "Production",
            "set 2 times",
            id="key-set-twice",
        ),
    ],
)
def test_refusals(
    script: ModuleType,
    verdict: dict[str, Any],
    config: str,
    env: str,
    reason: str,
) -> None:
    """Every refusal names why, and none of them produces a plan."""
    with pytest.raises(script.CutoverRefusedError, match=reason):
        script.plan_cutover(verdict, config, env)


def test_a_change_outside_the_four_keys_is_reported(script: ModuleType) -> None:
    """The guard `main` runs on the rewrite, exercised on rewrites gone wrong."""
    cutover = script.plan_cutover(VERDICT, PRODUCTION_ARMED, "Production")
    stray = PRODUCTION_CUT_OVER.replace(
        "aws:region: us-east-1", "aws:region: us-west-2"
    )
    kept = PRODUCTION_CUT_OVER.replace(
        "secretsprovider:", "  omnigraph:migrate_to_prefix: fmt9\nsecretsprovider:"
    )
    wrong_value = PRODUCTION_CUT_OVER.replace(
        "storage_prefix: fmt9", "storage_prefix: fmt6"
    )

    assert script.unexpected_changes(PRODUCTION_ARMED, stray, cutover) == [
        "unexpected +  aws:region: us-west-2",
        "unexpected -  aws:region: us-east-1",
    ]
    assert script.unexpected_changes(PRODUCTION_ARMED, kept, cutover) == [
        "missing -  omnigraph:migrate_to_prefix: fmt9",
    ]
    assert script.unexpected_changes(PRODUCTION_ARMED, wrong_value, cutover) == [
        "missing +  omnigraph:storage_prefix: fmt9",
        "missing -  omnigraph:storage_prefix: fmt6",
    ]


def test_the_body_carries_the_verdicts_evidence(script: ModuleType) -> None:
    """Totals, per-graph collapses, both roots and both binaries."""
    cutover = script.plan_cutover(VERDICT, PRODUCTION_ARMED, "Production")
    body = script.render_body(
        VERDICT,
        cutover,
        "s3://ol-data-witan-production/migrations/fmt9/verdict.json",
    )

    assert "rebuilt all 2 Production graphs" in body
    assert "format 6 -> 9 uniformly. 18,640 -> 18,580 rows." in body
    assert "(ParentOf 1, Tagged 59), not loss." in body
    assert "| council | 1,640 | 1,580 | ParentOf 1, Tagged 59 |" in body
    assert "| code-agent-kit | 17,000 | 17,000 | - |" in body
    assert "`s3://ol-data-witan-production/fmt6` is untouched" in body
    assert "New binary: `omnigraph 0.11.0`" in body
