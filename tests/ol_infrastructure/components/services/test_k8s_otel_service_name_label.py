"""Tests for the ol.mit.edu/otel-service-name label on webapp Deployments.

TempoServiceNeverSeen joins this label against Tempo's span metrics, so a label
that names a different service than the app reports as would fire forever, and
a missing one silently exempts the app from the check.
"""

from __future__ import annotations

from unittest.mock import sentinel

from ol_infrastructure.components.services.k8s import otel_service_name_label

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
    """An app with no OTel config (micromasters, xpro) has nothing to check."""
    assert otel_service_name_label({"FOO": "bar"}) == {}


def test_unresolved_value_means_no_label():
    """A non-string value (an Output) can't be read as a label value here."""
    assert otel_service_name_label({"OTEL_SERVICE_NAME": sentinel.output}) == {}
