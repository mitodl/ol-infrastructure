"""Tests for the ol.mit.edu/version label on OLApplicationK8s pods.

Release-workflow apps deploy by image digest, so the image reference no longer
says which release a pod runs. The calver goes on the pods as a label instead.
Every Deployment here selects on its full label set and the selector is
immutable, so the label has to stay off every selector: a version that reached
one would delete and recreate the Deployment on every release.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pulumi
import pulumi_kubernetes as kubernetes
import pytest

# Python 3.14+ compatibility
try:
    asyncio.get_event_loop()
except RuntimeError:
    asyncio.set_event_loop(asyncio.new_event_loop())


class K8sMocks(pulumi.runtime.Mocks):
    """Echo resource inputs back as outputs; no provider calls are expected."""

    def new_resource(self, args: pulumi.runtime.MockResourceArgs):
        """Return the inputs as the resource's state."""
        return (args.name + "_id", args.inputs)

    def call(self, args: pulumi.runtime.MockCallArgs):  # noqa: ARG002
        """Answer any provider function with an empty result."""
        return ({}, None)


pulumi.runtime.set_mocks(K8sMocks())

from ol_infrastructure.components.services.k8s import (  # noqa: E402
    RELEASE_VERSION_LABEL,
    OLApplicationK8s,
    OLApplicationK8sCeleryBeatConfig,
    OLApplicationK8sCeleryWorkerConfig,
    OLApplicationK8sConfig,
    OLApplicationK8sDevShellConfig,
    OLApplicationK8sScheduledJobConfig,
)

VERSION = "2026.10.28.1"


def _config(**overrides: Any) -> OLApplicationK8sConfig:
    kwargs: dict[str, Any] = {
        "application_name": "verapp",
        "application_namespace": "verapp-ns",
        "application_image_repository": "registry.example.com/verapp",
        "application_image_digest": "sha256:" + "a" * 64,
        "application_version": VERSION,
        "application_lb_service_name": "verapp-service",
        "application_lb_service_port_name": "http",
        "application_config": {},
        "env_from_secret_names": [],
        "project_root": Path("/nonexistent"),
        "import_nginx_config": False,
        "k8s_global_labels": {"ol.mit.edu/service": "verapp"},
        "registry": "direct",
        "manage_webapp_autoscaler": False,
        "manage_celery_autoscalers": False,
        "manage_pod_monitor": False,
        "manage_webapp_memory_vpa": False,
        "celery_worker_configs": [
            OLApplicationK8sCeleryWorkerConfig(queue_name="default")
        ],
        "celery_beat_config": OLApplicationK8sCeleryBeatConfig(),
        "pre_deploy_commands": [("migrate", ["python", "manage.py", "migrate"])],
        "post_deploy_commands": [("warm", ["python", "manage.py", "warm"])],
        "scheduled_jobs": [
            OLApplicationK8sScheduledJobConfig(
                name="reindex", schedule="30 4 * * 0", command=["true"]
            )
        ],
        "dev_shell_config": OLApplicationK8sDevShellConfig(),
    }
    return OLApplicationK8sConfig(**(kwargs | overrides))


def _deployment_labels(
    deployment: kubernetes.apps.v1.Deployment | None,
) -> pulumi.Output[list[dict[str, str]]]:
    assert deployment is not None
    return pulumi.Output.all(
        deployment.metadata.labels,
        deployment.spec.template.metadata.labels,
        deployment.spec.selector.match_labels,
    )


def _assert_on_pods_not_selector(expected: str | None, *, on_metadata: bool = True):
    def check(args: list[dict[str, str]]) -> None:
        metadata_labels, template_labels, selector_labels = args
        assert template_labels.get(RELEASE_VERSION_LABEL) == expected
        if on_metadata:
            assert metadata_labels.get(RELEASE_VERSION_LABEL) == expected
        assert RELEASE_VERSION_LABEL not in selector_labels

    return check


@pulumi.runtime.test
def test_webapp_pods_and_deployment_carry_the_version():
    """Kubewatch reads the Deployment's own labels for the promote button."""
    app = OLApplicationK8s(_config())
    return _deployment_labels(app.application_deployment).apply(
        _assert_on_pods_not_selector(VERSION)
    )


@pulumi.runtime.test
def test_celery_worker_and_beat_pods_carry_the_version():
    """Workers and beat run the same image, so they run the same release."""
    app = OLApplicationK8s(_config(application_name="verworker"))
    (worker,) = app.celery_deployments
    return pulumi.Output.all(
        _deployment_labels(worker).apply(_assert_on_pods_not_selector(VERSION)),
        _deployment_labels(app.beat_deployment).apply(
            _assert_on_pods_not_selector(VERSION)
        ),
    )


@pulumi.runtime.test
def test_dev_shell_pod_carries_the_version_but_its_selector_does_not():
    """The dev shell selects on its own labels, so only the template gets it."""
    app = OLApplicationK8s(_config(application_name="vershell"))
    return _deployment_labels(app.dev_shell_deployment).apply(
        _assert_on_pods_not_selector(VERSION, on_metadata=False)
    )


@pulumi.runtime.test
def test_job_pods_carry_the_version():
    """Deploy jobs and scheduled jobs run the release's image too."""
    app = OLApplicationK8s(_config(application_name="verjobs"))
    assert app.pre_deploy_job is not None
    assert app.post_deploy_job is not None

    def check(labels: list[dict[str, str]]) -> None:
        for pod_labels in labels:
            assert pod_labels[RELEASE_VERSION_LABEL] == VERSION

    return pulumi.Output.all(
        app.pre_deploy_job.spec.template.metadata.labels,
        app.post_deploy_job.spec.template.metadata.labels,
        app.scheduled_jobs[0].spec.job_template.spec.template.metadata.labels,
    ).apply(check)


@pulumi.runtime.test
def test_no_version_means_no_label():
    """CI deploys a master build, which has no release version."""
    app = OLApplicationK8s(
        _config(application_name="vernone", application_version=None)
    )
    return _deployment_labels(app.application_deployment).apply(
        _assert_on_pods_not_selector(None)
    )


@pytest.mark.parametrize(
    "version", ["", "2026.10.28.1 ", "-2026.10.28.1", "v" * 64, "2026/10/28"]
)
def test_version_that_is_not_a_label_value_is_rejected(version):
    """Kubernetes would reject it at apply time, after the stack half-updated."""
    with pytest.raises(ValueError, match="application_version"):
        _config(application_version=version)


def test_version_in_global_labels_is_rejected():
    """It would land in every selector and replace the Deployments."""
    with pytest.raises(ValueError, match="selector is immutable"):
        _config(k8s_global_labels={RELEASE_VERSION_LABEL: VERSION})
