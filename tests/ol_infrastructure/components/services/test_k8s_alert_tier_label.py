"""Tests for the ol.mit.edu/alert_tier label on OLApplicationK8s Deployments.

Alert rules join kube_pod_labels (the pod template) and kube_deployment_labels
(the Deployment's metadata) to find a workload's tier, so the label has to be on
both. It must stay off the selector: every Deployment here selects on its full
label set, the selector is immutable, and a tier that reached it would delete
and recreate the webapp instead of rolling it.
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
    ALERT_TIER_LABEL,
    OLApplicationK8s,
    OLApplicationK8sCeleryBeatConfig,
    OLApplicationK8sCeleryWorkerConfig,
    OLApplicationK8sConfig,
)
from ol_infrastructure.lib.ol_types import AlertTier  # noqa: E402


def _config(**overrides: Any) -> OLApplicationK8sConfig:
    kwargs: dict[str, Any] = {
        "application_name": "tierapp",
        "application_namespace": "tierapp-ns",
        "application_image_repository": "registry.example.com/tierapp",
        "application_docker_tag": "abc123",
        "application_lb_service_name": "tierapp-service",
        "application_lb_service_port_name": "http",
        "application_config": {},
        "env_from_secret_names": [],
        "project_root": Path("/nonexistent"),
        "import_nginx_config": False,
        "k8s_global_labels": {"ol.mit.edu/service": "tierapp"},
        "registry": "direct",
        "manage_webapp_autoscaler": False,
        "manage_celery_autoscalers": False,
        "manage_pod_monitor": False,
        "manage_webapp_memory_vpa": False,
        "celery_worker_configs": [
            OLApplicationK8sCeleryWorkerConfig(queue_name="default")
        ],
        "celery_beat_config": OLApplicationK8sCeleryBeatConfig(),
    }
    return OLApplicationK8sConfig(**(kwargs | overrides))


def _labels(
    deployment: kubernetes.apps.v1.Deployment | None,
) -> pulumi.Output[list[dict[str, str]]]:
    assert deployment is not None
    return pulumi.Output.all(
        deployment.metadata.labels,
        deployment.spec.template.metadata.labels,
        deployment.spec.selector.match_labels,
    )


def _assert_tier(expected: AlertTier):
    def check(args: list[dict[str, str]]) -> None:
        metadata_labels, template_labels, selector_labels = args
        assert metadata_labels[ALERT_TIER_LABEL] == expected
        assert template_labels[ALERT_TIER_LABEL] == expected
        assert ALERT_TIER_LABEL not in selector_labels

    return check


@pulumi.runtime.test
def test_webapp_pages_by_default():
    """The webapp is the user-facing workload."""
    app = OLApplicationK8s(_config())
    return _labels(app.application_deployment).apply(_assert_tier(AlertTier.page))


@pulumi.runtime.test
def test_celery_workers_notify_by_default():
    """A worker failure delays tasks; it is not a wake-up."""
    app = OLApplicationK8s(_config(application_name="tierworker"))
    (worker,) = app.celery_deployments
    return _labels(worker).apply(_assert_tier(AlertTier.notify))


@pulumi.runtime.test
def test_celery_beat_shares_the_celery_tier():
    """Beat is labelled component=celery, so it follows the same tier."""
    app = OLApplicationK8s(_config(application_name="tierbeat"))
    return _labels(app.beat_deployment).apply(_assert_tier(AlertTier.notify))


@pulumi.runtime.test
def test_tiers_are_overridable_per_app():
    """An internal tool's webapp need not page."""
    app = OLApplicationK8s(
        _config(
            application_name="tieroverride",
            webapp_alert_tier=AlertTier.notify,
            celery_alert_tier=AlertTier.ticket,
        )
    )
    (worker,) = app.celery_deployments
    return pulumi.Output.all(
        _labels(app.application_deployment).apply(_assert_tier(AlertTier.notify)),
        _labels(worker).apply(_assert_tier(AlertTier.ticket)),
        _labels(app.beat_deployment).apply(_assert_tier(AlertTier.ticket)),
    )


def test_tier_in_global_labels_is_rejected():
    """It would land in every selector and replace the Deployments."""
    with pytest.raises(ValueError, match="selector is immutable"):
        _config(k8s_global_labels={ALERT_TIER_LABEL: "page"})
