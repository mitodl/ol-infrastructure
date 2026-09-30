"""Tests for running OLApplicationK8s on a cluster that is not EKS.

A local k3d cluster has no AWS VPC CNI, no ECR access, and none of the KEDA,
VPA or Prometheus operator CRDs. Each of those dependencies has a config knob
that turns it off. These tests pin two things: that the knobs remove exactly
the resources a plain cluster cannot accept, and that leaving them at their
defaults still produces everything the deployed stacks rely on.
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

import pytest  # noqa: E402
from pydantic import ValidationError  # noqa: E402

from bridge.lib.versions import NGINX_VERSION  # noqa: E402
from ol_infrastructure.components.services.k8s import (  # noqa: E402
    GranianConfig,
    OLApplicationK8s,
    OLApplicationK8sCeleryWorkerConfig,
    OLApplicationK8sConfig,
    OLApplicationK8sDevShellConfig,
    OLApplicationK8sKedaWebappScalingConfig,
    OLApplicationK8sScheduledJobConfig,
)

SECURITY_GROUP_LABEL = "ol.mit.edu/pod-security-group"
LOCAL_IMAGE_REPOSITORY = "k3d-registry.localhost:5001/mitodl/myapp"


def _nginx_project_root(tmp_path):
    (tmp_path / "files").mkdir()
    (tmp_path / "files" / "web.conf_granian").write_text("server { listen 8071; }\n")
    return tmp_path


def _worker(**overrides) -> OLApplicationK8sCeleryWorkerConfig:
    defaults = {
        "queue_name": "default",
        "redis_host": pulumi.Output.from_input("redis.example.com"),
        "redis_password": "not-a-real-password",  # pragma: allowlist secret
    }
    defaults.update(overrides)
    return OLApplicationK8sCeleryWorkerConfig(**defaults)


def _deployed_config(**overrides) -> OLApplicationK8sConfig:
    """Return a config shaped like the ones the deployed stacks pass."""
    defaults = {
        "application_name": "myapp",
        "application_namespace": "myapp-ns",
        "application_image_repository": "mitodl/myapp",
        "application_docker_tag": "abc123",
        "application_security_group_id": pulumi.Output.from_input("sg-test"),
        "application_security_group_name": pulumi.Output.from_input("myapp-sg"),
        "application_lb_service_name": "myapp-service",
        "application_lb_service_port_name": "http",
        "application_config": {},
        "env_from_secret_names": ["myapp-secret"],
        "vault_k8s_resource_auth_name": "myapp-vault-auth",
        "project_root": "/tmp/myapp",  # noqa: S108
        "import_nginx_config": False,
        "k8s_global_labels": {"ol.mit.edu/application": "myapp"},
    }
    defaults.update(overrides)
    return OLApplicationK8sConfig(**defaults)


def _local_config(**overrides) -> OLApplicationK8sConfig:
    """Return a config with every EKS-only dependency turned off."""
    defaults = {
        "application_name": "myapp",
        "application_namespace": "myapp-ns",
        "application_image_repository": LOCAL_IMAGE_REPOSITORY,
        "application_docker_tag": "tilt-abc123",
        "application_lb_service_name": "myapp-service",
        "application_lb_service_port_name": "http",
        "application_config": {},
        "env_from_secret_names": ["myapp-secret"],
        "project_root": "/tmp/myapp",  # noqa: S108
        "import_nginx_config": False,
        "k8s_global_labels": {"ol.mit.edu/application": "myapp"},
        "registry": "direct",
        "manage_webapp_autoscaler": False,
        "manage_celery_autoscalers": False,
        "manage_pod_monitor": False,
        "manage_webapp_memory_vpa": False,
    }
    defaults.update(overrides)
    return OLApplicationK8sConfig(**defaults)


# ─── Config validation ────────────────────────────────────────────────────────


def test_security_group_and_vault_auth_are_optional():
    cfg = _local_config()
    assert cfg.application_security_group_id is None
    assert cfg.application_security_group_name is None
    assert cfg.vault_k8s_resource_auth_name is None


@pytest.mark.parametrize(
    "field", ["application_security_group_id", "application_security_group_name"]
)
def test_security_group_requires_both_id_and_name(field):
    with pytest.raises(ValidationError, match="must be set together"):
        _local_config(**{field: pulumi.Output.from_input("only-one")})


def test_keda_webapp_config_rejected_without_an_autoscaler():
    with pytest.raises(ValidationError, match="manage_webapp_autoscaler"):
        _local_config(
            webapp_keda_config=OLApplicationK8sKedaWebappScalingConfig(
                triggers=[{"type": "cpu", "metadata": {"value": "60"}}]
            )
        )


def test_workers_need_no_redis_without_celery_autoscalers():
    cfg = _local_config(
        celery_worker_configs=[_worker(redis_host=None, redis_password=None)]
    )

    assert cfg.celery_worker_configs[0].redis_host is None


@pytest.mark.parametrize("field", ["redis_host", "redis_password"])
def test_celery_autoscalers_require_redis_on_every_worker(field):
    workers = [_worker(worker_name="complete"), _worker(worker_name="partial")]
    setattr(workers[1], field, None)

    with pytest.raises(ValidationError, match="Missing on: partial"):
        _deployed_config(celery_worker_configs=workers)


def test_eks_wiring_defaults_are_on():
    cfg = _deployed_config()
    assert cfg.registry == "ecr"
    assert cfg.manage_webapp_autoscaler
    assert cfg.manage_celery_autoscalers
    assert cfg.manage_pod_monitor
    assert cfg.manage_webapp_memory_vpa


# ─── Local cluster: EKS-only resources are absent ─────────────────────────────


def test_local_config_creates_no_eks_only_resources():
    app = OLApplicationK8s(
        _local_config(
            application_name="local-none",
            granian_config=GranianConfig(application_module="main.wsgi:application"),
            celery_worker_configs=[_worker()],
        )
    )
    assert app.security_group_policy is None
    assert app.webapp_autoscaler is None
    assert app.celery_scaled_objects == []
    assert app.webapp_pod_monitor is None
    assert len(app.celery_deployments) == 1


@pulumi.runtime.test
def test_local_pods_carry_no_security_group_label():
    app = OLApplicationK8s(
        _local_config(
            application_name="local-labels",
            celery_worker_configs=[_worker()],
            scheduled_jobs=[
                OLApplicationK8sScheduledJobConfig(
                    name="reindex", schedule="30 4 * * 0", command=["true"]
                )
            ],
            dev_shell_config=OLApplicationK8sDevShellConfig(),
        )
    )

    def check(label_sets):
        for labels in label_sets:
            assert SECURITY_GROUP_LABEL not in labels

    return pulumi.Output.all(
        app.application_deployment.spec.template.metadata.labels,
        app.celery_deployments[0].spec.template.metadata.labels,
        app.scheduled_jobs[0].spec.job_template.spec.template.metadata.labels,
        app.dev_shell_deployment.spec.template.metadata.labels,
    ).apply(check)


@pulumi.runtime.test
def test_direct_registry_uses_images_as_given(tmp_path):
    app = OLApplicationK8s(
        _local_config(
            application_name="local-images",
            project_root=_nginx_project_root(tmp_path),
            import_nginx_config=True,
            granian_config=GranianConfig(application_module="main.wsgi:application"),
        )
    )

    def check(containers):
        images = {c["name"]: c["image"] for c in containers}
        assert images["nginx"] == f"nginx:{NGINX_VERSION}"
        assert images["local-images-app"] == f"{LOCAL_IMAGE_REPOSITORY}:tilt-abc123"

    return app.application_deployment.spec.template.spec.containers.apply(check)


@pulumi.runtime.test
def test_metrics_port_stays_without_pod_monitor():
    """The local pod keeps the deployed container shape; only the CRD is dropped."""
    app = OLApplicationK8s(
        _local_config(
            application_name="local-metrics",
            granian_config=GranianConfig(application_module="main.wsgi:application"),
        )
    )

    def check(containers):
        port_names = [p.get("name") for p in containers[0]["ports"]]
        assert "metrics" in port_names

    return app.application_deployment.spec.template.spec.containers.apply(check)


@pulumi.runtime.test
def test_replicas_are_fixed_without_autoscalers():
    expected_webapp_replicas = 1
    expected_worker_replicas = 2
    app = OLApplicationK8s(
        _local_config(
            application_name="local-replicas",
            application_min_replicas=expected_webapp_replicas,
            celery_worker_configs=[_worker(min_replicas=expected_worker_replicas)],
        )
    )

    def check(replicas):
        webapp_replicas, worker_replicas = replicas
        assert webapp_replicas == expected_webapp_replicas
        assert worker_replicas == expected_worker_replicas

    return pulumi.Output.all(
        app.application_deployment.spec.replicas,
        app.celery_deployments[0].spec.replicas,
    ).apply(check)


@pulumi.runtime.test
def test_one_worker_can_consume_every_queue():
    """The local default: one Deployment for all queues, named independently.

    The queue list is comma-joined for celery's -Q, so worker_name has to be
    set explicitly. It becomes part of the Deployment name and a label value,
    and a comma is valid in neither.
    """
    app = OLApplicationK8s(
        _local_config(
            application_name="local-merged",
            celery_worker_configs=[
                _worker(worker_name="all", queue_name="celery,hubspot_sync")
            ],
        )
    )
    assert app.celery_deployment_names == ["local-merged-all-celery-worker"]

    def check(args):
        containers, labels = args
        command = containers[0]["command"]
        assert command[command.index("-Q") + 1] == "celery,hubspot_sync"
        assert labels["ol.mit.edu/worker-name"] == "all"

    return pulumi.Output.all(
        app.celery_deployments[0].spec.template.spec.containers,
        app.celery_deployments[0].spec.template.metadata.labels,
    ).apply(check)


# ─── Deployed stacks: defaults keep every EKS resource ────────────────────────


def test_deployed_config_creates_eks_resources():
    app = OLApplicationK8s(
        _deployed_config(
            application_name="deployed-all",
            granian_config=GranianConfig(application_module="main.wsgi:application"),
            celery_worker_configs=[_worker()],
        )
    )
    assert app.security_group_policy is not None
    assert app.webapp_autoscaler is not None
    assert len(app.celery_scaled_objects) == 1
    assert app.webapp_pod_monitor is not None


@pulumi.runtime.test
def test_deployed_replicas_are_left_to_the_autoscalers():
    app = OLApplicationK8s(
        _deployed_config(
            application_name="deployed-replicas", celery_worker_configs=[_worker()]
        )
    )

    def check(replicas):
        assert replicas == [None, None]

    return pulumi.Output.all(
        app.application_deployment.spec.replicas,
        app.celery_deployments[0].spec.replicas,
    ).apply(check)


@pulumi.runtime.test
def test_deployed_images_and_labels_are_unchanged(tmp_path):
    app = OLApplicationK8s(
        _deployed_config(
            application_name="deployed-images",
            project_root=_nginx_project_root(tmp_path),
            import_nginx_config=True,
            granian_config=GranianConfig(application_module="main.wsgi:application"),
        )
    )

    def check(args):
        containers, labels = args
        images = {c["name"]: c["image"] for c in containers}
        assert images["nginx"].endswith(f"/dockerhub/library/nginx:{NGINX_VERSION}")
        assert images["deployed-images-app"].endswith(
            ".amazonaws.com/mitodl/myapp:abc123"
        )
        assert labels[SECURITY_GROUP_LABEL] == "myapp-sg"

    return pulumi.Output.all(
        app.application_deployment.spec.template.spec.containers,
        app.application_deployment.spec.template.metadata.labels,
    ).apply(check)
