"""Tests for the ol.mit.edu/otel-service-name label on webapp Deployments.

TempoServiceNeverSeen joins this label against Tempo's span metrics, so a label
that names a different service than the app reports as would fire forever, and
a missing one silently exempts the app from the check. The label must also stay
off the selector (immutable, so changing it replaces the Deployment) and the pod
template (changing it rolls every pod).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import sentinel

import pulumi

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
    OLApplicationK8s,
    OLApplicationK8sConfig,
    otel_service_name_label,
)

LABEL = "ol.mit.edu/otel-service-name"


def test_otel_service_name_is_used():
    """ocw-studio and edxapp set the SDK variable directly."""
    assert otel_service_name_label({"OTEL_SERVICE_NAME": "ocw-studio-webapp"}) == {
        LABEL: "ocw-studio-webapp"
    }


def test_django_setting_is_used_when_the_sdk_var_is_absent():
    """The mitol-django-observability apps name themselves this way."""
    assert otel_service_name_label(
        {"OPENTELEMETRY_SERVICE_NAME": "mitxonline-webapp"}
    ) == {LABEL: "mitxonline-webapp"}


def test_sdk_var_wins_over_django_setting():
    """Matches telemetry.py, which is what decides the emitted service.name."""
    assert otel_service_name_label(
        {
            "OTEL_SERVICE_NAME": "from-sdk",
            "OPENTELEMETRY_SERVICE_NAME": "from-setting",
        }
    ) == {LABEL: "from-sdk"}


def test_no_otel_config_means_no_label():
    """An app with no OTel config (micromasters, mitxpro) has nothing to check."""
    assert otel_service_name_label({"FOO": "bar"}) == {}


def test_unresolved_value_means_no_label():
    """A non-string value (an Output) can't be read as a label value here."""
    assert otel_service_name_label({"OTEL_SERVICE_NAME": sentinel.output}) == {}


@pulumi.runtime.test
def test_label_is_on_deployment_metadata_only():
    """Selector and pod template must not carry it, or applying it churns pods."""
    app = OLApplicationK8s(
        OLApplicationK8sConfig(
            application_name="otelapp",
            application_namespace="otelapp-ns",
            application_image_repository="registry.example.com/otelapp",
            application_docker_tag="abc123",
            application_lb_service_name="otelapp-service",
            application_lb_service_port_name="http",
            application_config={"OPENTELEMETRY_SERVICE_NAME": "otelapp-webapp"},
            env_from_secret_names=[],
            project_root=Path("/nonexistent"),
            import_nginx_config=False,
            k8s_global_labels={"ol.mit.edu/application": "otelapp"},
            registry="direct",
            manage_webapp_autoscaler=False,
            manage_celery_autoscalers=False,
            manage_pod_monitor=False,
            manage_webapp_memory_vpa=False,
        )
    )

    def check(args):
        metadata_labels, selector_labels, template_labels = args
        assert metadata_labels[LABEL] == "otelapp-webapp"
        assert LABEL not in selector_labels
        assert LABEL not in template_labels

    deployment = app.application_deployment
    return pulumi.Output.all(
        deployment.metadata.labels,
        deployment.spec.selector.match_labels,
        deployment.spec.template.metadata.labels,
    ).apply(check)
