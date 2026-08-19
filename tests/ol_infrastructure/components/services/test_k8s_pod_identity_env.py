"""Tests for the pod-identity env vars on application deployments.

The ordering assertion is the point. OTEL_RESOURCE_ATTRIBUTES refers to
``$(KUBERNETES_POD_NAME)`` for service.instance.id, and the kubelet resolves a
``$(VAR)`` reference only against entries defined EARLIER in the same
container's env list. Emit the downward-API entries after the application
config and the reference ships as the literal seven characters instead -- which
is the bug that put the string "${HOSTNAME}" in Tempo across every affected
service.
"""

from __future__ import annotations

from ol_infrastructure.components.services.k8s import (
    POD_IDENTITY_FIELD_REFS,
    build_application_env_vars,
)


def _names(env_vars) -> list[str]:
    return [env_var.name for env_var in env_vars]


def test_pod_identity_vars_come_first():
    """They must precede anything from application_config, not merely exist."""
    env_vars = build_application_env_vars(
        {
            "OTEL_RESOURCE_ATTRIBUTES": (
                "service.namespace=learn,service.instance.id=$(KUBERNETES_POD_NAME)"
            ),
        }
    )

    assert _names(env_vars)[:3] == [
        "KUBERNETES_POD_NAME",
        "KUBERNETES_NAMESPACE",
        "KUBERNETES_NODE_NAME",
    ]


def test_pod_identity_precedes_the_var_that_references_it():
    """Stated as the relationship that actually matters, not just an index."""
    env_vars = build_application_env_vars(
        {"OTEL_RESOURCE_ATTRIBUTES": "service.instance.id=$(KUBERNETES_POD_NAME)"}
    )
    names = _names(env_vars)

    assert names.index("KUBERNETES_POD_NAME") < names.index("OTEL_RESOURCE_ATTRIBUTES")


def test_pod_identity_vars_use_the_downward_api():
    """A literal value here would defeat the whole point."""
    env_vars = build_application_env_vars({})
    by_name = {env_var.name: env_var for env_var in env_vars}

    for env_var_name, field_path in POD_IDENTITY_FIELD_REFS:
        env_var = by_name[env_var_name]
        assert env_var.value is None
        assert env_var.value_from.field_ref.field_path == field_path


def test_application_config_and_port_are_still_emitted():
    """The extraction must not drop what the deployment already relied on."""
    env_vars = build_application_env_vars({"FOO": "bar"})
    by_name = {env_var.name: env_var for env_var in env_vars}

    assert by_name["FOO"].value == "bar"
    assert "PORT" in by_name
