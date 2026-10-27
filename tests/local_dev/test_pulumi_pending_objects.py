"""Tests for local-dev/scripts/pulumi-pending-objects.py."""

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

SCRIPT = (
    Path(__file__).resolve().parents[2] / "local-dev/scripts/pulumi-pending-objects.py"
)


def _run(export: dict[str, Any]) -> str:
    return subprocess.run(  # noqa: S603
        [sys.executable, str(SCRIPT)],
        input=json.dumps(export),
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def _pending(kind: str, api_type: str, name: str) -> dict[str, Any]:
    return {
        "type": kind,
        "resource": {
            "type": f"kubernetes:{api_type}",
            "inputs": {"metadata": {"name": name}},
        },
    }


def test_nothing_pending_prints_nothing():
    assert _run({"deployment": {"resources": []}}) == ""


def test_pending_k8s_creates_print_kind_slash_name_and_nothing_else_does():
    export = {
        "deployment": {
            "pending_operations": [
                _pending("creating", "batch/v1:Job", "mitxonline-app-pre-deploy"),
                _pending("creating", "apisix.apache.org/v2:ApisixRoute", "r-dev"),
                _pending("deleting", "core/v1:Secret", "mitxonline-secrets"),
                {
                    "type": "creating",
                    "resource": {"type": "pulumi:providers:kubernetes", "inputs": {}},
                },
            ]
        }
    }

    assert _run(export) == "job/mitxonline-app-pre-deploy apisixroute/r-dev\n"
