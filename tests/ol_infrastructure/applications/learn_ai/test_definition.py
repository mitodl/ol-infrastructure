"""Tests for the learn-ai application definition.

The definition is called by the deployed program and is shaped for a local one
too. These tests render it with bindings shaped like a local k3d cluster (no
AWS, Vault, KEDA, VPA or Prometheus operator) and like the deployed stacks, and
pin what the bindings are allowed to change.
"""

from __future__ import annotations

import asyncio

import pulumi

# Python 3.14+ compatibility
try:
    asyncio.get_event_loop()
except RuntimeError:
    asyncio.set_event_loop(asyncio.new_event_loop())


class K8sMocks(pulumi.runtime.Mocks):
    def new_resource(self, args: pulumi.runtime.MockResourceArgs):
        return [args.name + "_id", args.inputs]

    def call(self, args: pulumi.runtime.MockCallArgs):  # noqa: ARG002
        return {}


pulumi.runtime.set_mocks(K8sMocks())

import pulumi_kubernetes as kubernetes  # noqa: E402
import pytest  # noqa: E402
from pydantic import ValidationError  # noqa: E402

from ol_infrastructure.applications.learn_ai import definition  # noqa: E402
from ol_infrastructure.components.services.k8s import (  # noqa: E402
    OLApplicationK8s,
    OLApplicationK8sCeleryRedisConfig,
)
from ol_infrastructure.lib.ol_types import KubernetesServiceAppProtocol  # noqa: E402
from ol_infrastructure.lib.pulumi_helper import docker_image_config_kwargs  # noqa: E402

LOCAL_IMAGE_TAG = "tilt-abc123"
LOCAL_OIDC_SECRET = "ol-mitlearn-oidc"  # noqa: S105  # pragma: allowlist secret


def _local_bindings(**overrides) -> definition.LearnAiBindings:
    """Return bindings shaped like the ones a k3d program would build."""
    settings = {
        "env_suffix": "dev",
        "hostnames": definition.LearnAiHostnames(
            api="ai.learn.mit.test",
            frontend="ai-sandbox.learn.mit.test",
            learn_api="api.learn.mit.test",
        ),
        "environment_variables": {"MITOL_APP_BASE_URL": "https://ai.learn.mit.test"},
        "cors_frontend_origins": ["https://learn.mit.test"],
        "k8s_labels": {"ol.mit.edu/application": "learn-ai"},
        "secret_names": ["learn-ai-secrets"],
        "application_docker_tag": LOCAL_IMAGE_TAG,
        "cluster": definition.ClusterCapabilities(
            registry="direct", autoscalers=False, pod_monitors=False
        ),
        "min_replicas": 1,
        "sizing": definition.LearnAiSizing(
            web=definition.WorkloadResources(
                requests={"cpu": "100m", "memory": "512Mi"}, limits={"memory": "2Gi"}
            ),
            celery_beat=definition.WorkloadResources(
                requests={"cpu": "10m", "memory": "256Mi"}, limits={"memory": "1Gi"}
            ),
            celery_queues=dict.fromkeys(
                definition.CELERY_QUEUE_NAMES,
                definition.WorkloadResources(
                    requests={"cpu": "50m", "memory": "512Mi"},
                    limits={"memory": "1536Mi"},
                ),
            ),
        ),
        "celery_topology": "merged",
        "oidc_secret_name": LOCAL_OIDC_SECRET,
    }
    settings.update(overrides)
    return definition.LearnAiBindings(**settings)


def _deployed_bindings(**overrides) -> definition.LearnAiBindings:
    """Return bindings shaped like the ones the deployed program builds."""
    settings = {
        "cluster": definition.ClusterCapabilities(
            security_group_id=pulumi.Output.from_input("sg-test"),
            security_group_name=pulumi.Output.from_input("learn-ai-app"),
            vault_auth_name="learn-ai-auth",
        ),
        "service_account_name": pulumi.Output.from_input("learn-ai-admin"),
        "optional_secret_names": ["learn-ai-opik-keycloak"],
        "celery_redis": OLApplicationK8sCeleryRedisConfig(
            host=pulumi.Output.from_input("redis.example.com"),
            password="not-a-real-password",  # pragma: allowlist secret
        ),
        "celery_topology": "per-queue",
        "oidc_secret_name": None,
        "sizing": definition.LearnAiSizing(),
    }
    settings.update(overrides)
    return _local_bindings(**settings)


# ─── Environment ──────────────────────────────────────────────────────────────


def test_the_environment_is_the_shared_values_under_the_bindings():
    env = definition.environment_variables(
        _local_bindings(environment_variables={"A": "1"})
    )

    assert set(env) == {
        *definition.LOCAL_AND_DEPLOYED_ENV,
        "A",
        "OTEL_RESOURCE_ATTRIBUTES",
    }
    assert "ol.mit.edu/application=learn-ai" in env["OTEL_RESOURCE_ATTRIBUTES"]


def test_rendering_does_not_mutate_the_bindings_environment():
    given = {"MITOL_APP_BASE_URL": "https://ai.example"}
    bindings = _local_bindings(environment_variables=given)

    definition.environment_variables(bindings)

    assert bindings.environment_variables == given


# ─── Workloads ────────────────────────────────────────────────────────────────


def test_local_bindings_render_without_eks_only_resources():
    app = OLApplicationK8s(definition.application_config(_local_bindings()))

    assert app.security_group_policy is None
    assert app.webapp_autoscaler is None
    assert app.celery_scaled_objects == []
    assert app.webapp_pod_monitor is None
    assert app.beat_deployment is not None


def test_deployed_bindings_keep_the_eks_wiring():
    config = definition.application_config(_deployed_bindings())

    assert config.registry == "ecr"
    assert config.import_nginx_config is False
    assert config.manage_webapp_autoscaler
    assert config.manage_celery_autoscalers
    assert config.manage_webapp_memory_vpa
    assert config.manage_pod_monitor
    assert config.vault_k8s_resource_auth_name == "learn-ai-auth"
    assert config.optional_env_from_secret_names == ["learn-ai-opik-keycloak"]
    assert config.application_lb_service_app_protocol == KubernetesServiceAppProtocol.WS
    assert config.init_migrations
    assert config.init_collectstatic
    assert config.granian_config.interface == "asgi"
    assert config.granian_config.worker_startup_rss == 600


def test_release_pipeline_image_kwargs_reach_the_component(monkeypatch):
    """The release pipeline sends a digest and a version; both must get through.

    The bindings forbid unknown fields, so a kwarg docker_image_config_kwargs
    returns that the bindings lack fails every deployed stack.
    """
    digest = "sha256:" + "a" * 64
    monkeypatch.delenv("LEARN_AI_DOCKER_TAG", raising=False)
    monkeypatch.setenv("LEARN_AI_DOCKER_SHA", digest)
    monkeypatch.setenv("LEARN_AI_RELEASE_VERSION", "2026.10.28.1")

    config = definition.application_config(
        _local_bindings(
            application_docker_tag=None, **docker_image_config_kwargs("LEARN_AI")
        )
    )

    assert config.application_image_digest == digest
    assert config.application_version == "2026.10.28.1"


def test_bindings_reject_a_misspelled_field():
    with pytest.raises(ValidationError, match="granian_reload"):
        _local_bindings(granian_reload=True)


def test_deployed_topology_runs_one_worker_per_queue():
    workers = definition.application_config(_deployed_bindings()).celery_worker_configs

    assert [w.queue_name for w in workers] == ["default", "edx_content"]


def test_merged_topology_runs_one_worker_for_every_queue():
    (worker,) = definition.application_config(_local_bindings()).celery_worker_configs

    assert worker.queue_name == "default,edx_content"


def test_sizing_defaults_are_the_deployed_values():
    config = definition.application_config(_deployed_bindings())

    assert config.resource_requests == {"cpu": "100m", "memory": "1000Mi"}
    assert config.resource_limits == {"memory": "2048Mi"}
    assert config.celery_beat_config.resource_requests == {
        "cpu": "10m",
        "memory": "1000Mi",
    }
    assert config.celery_beat_config.resource_limits == {"memory": "1536Mi"}
    assert all(
        w.resource_requests == {"cpu": "100m", "memory": "1000Mi"}
        and w.resource_limits == {"memory": "1536Mi"}
        for w in config.celery_worker_configs
    )


def test_sizing_must_name_every_queue():
    size = definition.WorkloadResources(
        requests={"memory": "1Gi"}, limits={"memory": "1Gi"}
    )

    with pytest.raises(ValidationError, match=r"missing \['edx_content'\]"):
        definition.LearnAiSizing(celery_queues={"default": size})


def test_the_bound_volumes_reach_the_component_config():
    volume = kubernetes.core.v1.VolumeArgs(
        name="ca",
        config_map=kubernetes.core.v1.ConfigMapVolumeSourceArgs(
            name="local-dev-ca-bundle"
        ),
    )
    mount = kubernetes.core.v1.VolumeMountArgs(
        name="ca", mount_path="/etc/ssl/local-dev", read_only=True
    )

    config = definition.application_config(
        _local_bindings(extra_volumes=[volume], extra_volume_mounts=[mount])
    )

    assert config.extra_volumes == [volume]
    assert config.extra_volume_mounts == [mount]
