"""Every Deployment a local Pulumi program renders shows in one Tilt resource.

An app's Tiltfile names, in WORKLOADS, the Deployments that get a Tilt resource
of their own (tiltlib.star's pulumi_app_workloads) and the labels each resource
follows its pods by; the rest stay in the app's resource. The labels come from
the shared service component, so a rename there would leave a resource without
pods only after the next stack recreate, and with no error.
"""

import ast
from pathlib import Path
from typing import Any

import pytest

from tests.local_dev.program_render import DEPLOYMENT, render_program

REPO = Path(__file__).parents[2]
APPS = sorted(
    p.parent.parent.name for p in (REPO / "local-dev/apps").glob("*/pulumi/Pulumi.yaml")
)


def _tiltfile_literal(app: str, name: str) -> Any:
    tree = ast.parse((REPO / "local-dev/apps" / app / "Tiltfile").read_text())
    for node in tree.body:
        if isinstance(node, ast.Assign) and [
            getattr(target, "id", None) for target in node.targets
        ] == [name]:
            try:
                return ast.literal_eval(node.value)
            except ValueError:
                pytest.fail(f"{app}'s Tiltfile: {name} must be a literal")
    return pytest.fail(
        f"{app}'s Tiltfile declares no {name}; "
        "see pulumi_app_workloads in local-dev/tiltlib.star"
    )


@pytest.mark.parametrize("app", APPS)
def test_each_deployment_is_followed_by_one_tilt_resource(app, tmp_path, monkeypatch):
    workloads = _tiltfile_literal(app, "WORKLOADS")
    app_resource = _tiltfile_literal(app, "DEPLOY_NAME")
    pod_labels = {
        inputs["metadata"]["name"]: inputs["spec"]["template"]["metadata"]["labels"]
        for typ, _, inputs in render_program(app, tmp_path, monkeypatch).resources
        if typ == DEPLOYMENT
    }
    assert set(pod_labels) - {app_resource} == set(workloads)
    for deployment, labels in pod_labels.items():
        following = {
            name
            for name, workload in workloads.items()
            if workload["labels"].items() <= labels.items()
        }
        assert following == {deployment} & set(workloads), (
            f"{deployment}'s pods carry {labels}"
        )
