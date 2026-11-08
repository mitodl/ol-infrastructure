"""Every Deployment a local Pulumi program renders shows in exactly one Tilt resource.

pulumi-app-apply.sh keeps only some components' Deployments in the app
resource, and the app's Tiltfile gives each other Deployment its own resource
through tiltlib.star's pulumi_app_rows. The resources here come from running
that very function, under stubs for Tilt's builtins, with the arguments the
Tiltfile passes it (read with `ast`, so they have to be literals). Every
program under local-dev/apps/*/pulumi is checked, so an app that gets a
program, a worker or another Deployment is covered as soon as it renders.
"""

import ast
import os
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from tests.local_dev.program_render import DEPLOYMENT, render_program

REPO = Path(__file__).parents[2]
APPS = sorted(
    p.parent.parent.name for p in (REPO / "local-dev/apps").glob("*/pulumi/Pulumi.yaml")
)
COMPONENT = "ol.mit.edu/component"


def _components_in_the_app_row() -> set[str]:
    script = (REPO / "local-dev/scripts/pulumi-app-apply.sh").read_text()
    (listed,) = re.findall(rf"{re.escape(COMPONENT)} in \(([^)]*)\)", script)
    return {c.strip() for c in listed.split(",")}


def _rows(app: str) -> dict[str, dict[str, Any]]:
    """Resource name -> its pod selector and apply command, as pulumi_app_rows
    registers them for an app with a checkout at /checkout.
    """
    tree = ast.parse((REPO / "local-dev/apps" / app / "Tiltfile").read_text())
    calls = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Call) and getattr(n.func, "id", None) == "pulumi_app_rows"
    ]
    assert len(calls) == 1, f"{app}'s Tiltfile: {len(calls)} pulumi_app_rows calls"
    kwargs = {
        kw.arg: ast.literal_eval(kw.value)
        for kw in calls[0].keywords
        if kw.arg in ("workers", "beat", "extra_rows", "namespace")
    }

    rows: dict[str, dict[str, Any]] = {}

    def k8s_custom_deploy(name, apply_cmd, **_):
        rows[name] = {"cmd": apply_cmd}

    def k8s_resource(name, extra_pod_selectors, **_):
        (rows[name]["selector"],) = extra_pod_selectors

    tilt: dict[str, Any] = {
        "k8s_custom_deploy": k8s_custom_deploy,
        "k8s_resource": k8s_resource,
        "TRIGGER_MODE_MANUAL": object(),
        "config": SimpleNamespace(main_dir=str(REPO)),
        "os": os,
    }
    exec(  # noqa: S102
        compile((REPO / "local-dev/tiltlib.star").read_text(), "tiltlib.star", "exec"),
        tilt,
    )
    tilt["pulumi_app_rows"](app, app_dir="/checkout", **kwargs)
    return rows


def _options(cmd: list[str]) -> dict[str, str]:
    """Return the --flag value pairs of an apply command."""
    return {cmd[i]: cmd[i + 1] for i in range(len(cmd) - 1) if cmd[i].startswith("--")}


def _deployments(app: str, tmp_path, monkeypatch) -> dict[str, dict[str, Any]]:
    """Deployment name -> its labels, namespace, pod labels and container names."""
    mocks = render_program(app, tmp_path, monkeypatch)
    return {
        inputs["metadata"]["name"]: {
            "labels": inputs["metadata"]["labels"],
            "namespace": inputs["metadata"]["namespace"],
            "pod_labels": inputs["spec"]["template"]["metadata"]["labels"],
            "containers": [
                c["name"] for c in inputs["spec"]["template"]["spec"]["containers"]
            ],
        }
        for typ, _, inputs in mocks.resources
        if typ == DEPLOYMENT
    }


def _selecting(selector: str, deployments: dict[str, dict[str, Any]]) -> list[str]:
    """Names of the Deployments whose pods a kubectl label selector matches."""
    wanted = dict(pair.split("=", 1) for pair in selector.split(","))
    return [
        n for n, d in deployments.items() if wanted.items() <= d["pod_labels"].items()
    ]


@pytest.mark.parametrize("app", APPS)
def test_the_rows_are_exactly_the_deployments_the_app_row_leaves_out(
    app, tmp_path, monkeypatch
):
    """The apply script filters on the Deployments' own labels."""
    deployments = _deployments(app, tmp_path, monkeypatch)
    in_app_row = _components_in_the_app_row()
    left_out = {
        n
        for n, d in deployments.items()
        if d["labels"].get(COMPONENT) not in in_app_row
    }
    assert set(_rows(app)) == left_out


@pytest.mark.parametrize("app", APPS)
def test_each_row_selects_the_pods_of_its_deployment_only(app, tmp_path, monkeypatch):
    deployments = _deployments(app, tmp_path, monkeypatch)
    for row, spec in _rows(app).items():
        selected = [
            n
            for n, d in deployments.items()
            if spec["selector"].items() <= d["pod_labels"].items()
        ]
        assert selected == [row]


@pytest.mark.parametrize("app", APPS)
def test_each_apply_command_names_what_the_program_renders(app, tmp_path, monkeypatch):
    """The namespace, the container and the beat selector the script is given
    exist in the render; a wrong one would only fail at click time.
    """
    deployments = _deployments(app, tmp_path, monkeypatch)
    for row, spec in _rows(app).items():
        opts = _options(spec["cmd"])
        assert opts["--namespace"] == deployments[row]["namespace"]
        if spec["cmd"][1] == "now":
            assert _selecting(opts["--selector"], deployments) == [row]
            assert opts["--container"] in deployments[row]["containers"]
            if "--beat-selector" in opts:
                (beat,) = _selecting(opts["--beat-selector"], deployments)
                assert beat == opts["--beat-row"]
                assert "celery-beat" in deployments[beat]["containers"]
