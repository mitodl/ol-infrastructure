"""Tests for the promotion-gate age report."""

from __future__ import annotations

import importlib.machinery
import importlib.util
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ol_concourse.pipelines.infrastructure.promotion_gate_age.pipeline import (
    promotion_gate_age_pipeline,
)

SCRIPT_PATH = Path(__file__).resolve().parents[2] / "bin" / "promotion-gate-age"
NOW = datetime(2026, 8, 24, 14, 0, tzinfo=UTC)


def load_module():
    """Load the CLI directly; `bin/` is not a package and the file has no suffix."""
    loader = importlib.machinery.SourceFileLoader(
        "test_bin_promotion_gate_age", str(SCRIPT_PATH)
    )
    spec = importlib.util.spec_from_loader("test_bin_promotion_gate_age", loader)
    if spec is None or spec.loader is None:
        msg = f"Unable to load module from {SCRIPT_PATH}"
        raise RuntimeError(msg)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


gate_age = load_module()


def issue(
    number: int, title: str, created_at: str, **extra: object
) -> dict[str, object]:
    return {
        "number": number,
        "title": title,
        "html_url": f"https://github.mit.edu/x/y/issues/{number}",
        "created_at": created_at,
        "labels": [{"name": "promotion-to-production"}],
        **extra,
    }


def test_parse_gates_keeps_only_pulumi_issues():
    gates = gate_age.parse_gates(
        [
            issue(
                1,
                "[bot] Pulumi ol-infrastructure-dagster-server QA deployed.",
                "2026-08-21T17:31:18Z",
            ),
            issue(
                2, "[bot] MFE learning deployed to QA for mitx", "2026-08-01T00:00:00Z"
            ),
            issue(
                3,
                "[bot] Pulumi ol-application-witan Production ready to deploy.",
                "2026-08-20T00:00:00Z",
                pull_request={"url": "..."},
            ),
        ]
    )

    assert [gate.number for gate in gates] == [1]
    assert gates[0].opened_at == datetime(2026, 8, 21, 17, 31, 18, tzinfo=UTC)


def test_stale_gates_applies_threshold_and_sorts_oldest_first():
    gates = gate_age.parse_gates(
        [
            issue(1, "[bot] Pulumi a QA deployed.", "2026-08-23T12:00:00Z"),
            issue(2, "[bot] Pulumi b QA deployed.", "2026-08-24T13:00:00Z"),
            issue(3, "[bot] Pulumi c QA deployed.", "2026-08-21T17:31:18Z"),
        ]
    )

    stale = gate_age.stale_gates(gates, NOW, timedelta(hours=24))

    assert [gate.number for gate in stale] == [3, 1]


def test_format_age():
    assert gate_age.format_age(timedelta(days=2, hours=21, minutes=21)) == "2d 21h"
    assert gate_age.format_age(timedelta(hours=24)) == "1d"
    assert gate_age.format_age(timedelta(hours=5, minutes=59)) == "5h"


def test_gate_name_handles_both_gate_title_shapes():
    assert (
        gate_age.gate_name("[bot] Pulumi ol-infrastructure-dagster-server QA deployed.")
        == "ol-infrastructure-dagster-server QA"
    )
    assert (
        gate_age.gate_name(
            "[bot] Pulumi ol-application-witan Production ready to deploy."
        )
        == "ol-application-witan Production"
    )


def test_slack_text_names_each_gate_with_its_age():
    gates = gate_age.parse_gates(
        [
            issue(
                81258,
                "[bot] Pulumi ol-infrastructure-dagster-server QA deployed.",
                "2026-08-21T17:31:18Z",
            )
        ]
    )

    text = gate_age.slack_text(gates, NOW, timedelta(hours=24))

    assert "1 Pulumi promotion gate(s) open longer than 1d" in text
    assert (
        "<https://github.mit.edu/x/y/issues/81258|#81258> "
        "ol-infrastructure-dagster-server QA, open 2d 20h"
    ) in text
    assert "promotion-held" not in text


def test_held_gates_are_counted_not_listed():
    held_labels = [{"name": "promotion-to-production"}, {"name": "promotion-held"}]
    gates = gate_age.parse_gates(
        [
            issue(
                80297,
                "[bot] Pulumi ol-infrastructure-edxapp-application.xpro "
                "xpro.Production ready to deploy.",
                "2026-08-18T19:25:52Z",
                labels=held_labels,
            ),
            issue(
                81258,
                "[bot] Pulumi ol-infrastructure-dagster-server QA deployed.",
                "2026-08-21T17:31:18Z",
            ),
        ]
    )
    stale = gate_age.stale_gates(gates, NOW, timedelta(hours=24))

    text = gate_age.slack_text(stale, NOW, timedelta(hours=24))

    assert "1 Pulumi promotion gate(s)" in text
    assert "#80297" not in text
    assert (
        "Not listed: 1 gate(s) labelled `promotion-held`, the oldest open 5d 18h"
        in text
    )
    assert gate_age.slack_text(stale[:1], NOW, timedelta(hours=24)) is None


def test_pipeline_runs_the_script_on_weekdays():
    pipeline = promotion_gate_age_pipeline()

    (schedule,) = [r for r in pipeline.resources if r.type == "time"]
    assert "Saturday" not in schedule.source["days"]
    (job,) = pipeline.jobs
    task = job.plan[-1].config
    assert task.run.args == ["ol-infrastructure/bin/promotion-gate-age", "--post"]
