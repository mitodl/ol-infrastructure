"""Tests for the OLApplicationK8s developer shell Deployment.

The dev shell is a place to run manage.py commands that no autoscaler, VPA or
node consolidation can interrupt, so these tests pin the properties that make
it safe: it is off by default, it shares nothing with the webapp's selector,
it is created at zero replicas with the Recreate strategy, it carries the
do-not-disrupt annotation, and it runs the application image with the
application's env.
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

from ol_infrastructure.components.services.k8s import (  # noqa: E402
    OLApplicationK8s,
    OLApplicationK8sCeleryBeatConfig,
    OLApplicationK8sConfig,
    OLApplicationK8sDevShellConfig,
    application_deployment_names,
    dev_shell_deployment_name,
)


def _base_config(**overrides) -> OLApplicationK8sConfig:
    defaults = {
        "application_name": "myapp",
        "application_namespace": "myapp-ns",
        "application_image_repository": "registry.example.com/myapp",
        "application_docker_tag": "abc123",
        "application_security_group_id": pulumi.Output.from_input("sg-test"),
        "application_security_group_name": pulumi.Output.from_input("myapp-sg"),
        "application_service_account_name": "myapp-sa",
        "application_lb_service_name": "myapp-service",
        "application_lb_service_port_name": "http",
        "application_config": {},
        "env_from_secret_names": ["myapp-secret"],
        "vault_k8s_resource_auth_name": "myapp-vault-auth",
        "project_root": "/tmp/myapp",  # noqa: S108
        "import_nginx_config": False,
        "k8s_global_labels": {
            "ol.mit.edu/application": "myapp",
            "ol.mit.edu/environment": "qa",
        },
    }
    defaults.update(overrides)
    return OLApplicationK8sConfig(**defaults)


def test_dev_shell_defaults():
    cfg = OLApplicationK8sDevShellConfig()
    assert cfg.command == ["sleep", "infinity"]
    assert cfg.resource_limits["memory"] == "4Gi"
    assert cfg.resource_requests["memory"] == "4Gi"


def test_dev_shell_off_by_default():
    app = OLApplicationK8s(_base_config(application_name="noshell"))
    assert app.dev_shell_deployment is None
    assert app.dev_shell_deployment_name is None
    assert "noshell-dev-shell" not in app.all_deployment_names


def test_dev_shell_name_helper_and_deployment_names_agree():
    assert dev_shell_deployment_name("my_app") == "my-app-dev-shell"
    names = application_deployment_names(
        "my_app",
        celery_beat_config=OLApplicationK8sCeleryBeatConfig(),
        dev_shell_config=OLApplicationK8sDevShellConfig(),
    )
    assert names[-1] == "my-app-dev-shell"
    assert "my-app-dev-shell" not in application_deployment_names("my_app")


@pulumi.runtime.test
def test_dev_shell_is_included_in_restart_targets():
    app = OLApplicationK8s(
        _base_config(
            application_name="withshell",
            dev_shell_config=OLApplicationK8sDevShellConfig(),
        )
    )
    assert app.dev_shell_deployment_name == "withshell-dev-shell"
    assert app.all_deployment_names[-1] == "withshell-dev-shell"
    assert app.dev_shell_deployment is not None

    def check(name):
        assert name == "withshell-dev-shell"

    return app.dev_shell_deployment.metadata.name.apply(check)


@pulumi.runtime.test
def test_dev_shell_starts_at_zero_recreate_no_one_else_selects():
    app = OLApplicationK8s(
        _base_config(
            application_name="shellsel",
            dev_shell_config=OLApplicationK8sDevShellConfig(),
        )
    )
    assert app.dev_shell_deployment is not None

    def check(args):
        spec, webapp_selector = args
        assert spec["replicas"] == 0
        assert spec["strategy"]["type"] == "Recreate"
        shell_labels = spec["selector"]["match_labels"]
        assert shell_labels["ol.mit.edu/component"] == "dev-shell"
        # A pod carrying the webapp's selector labels is counted by its HPA/KEDA.
        assert not all(shell_labels.get(k) == v for k, v in webapp_selector.items())
        assert shell_labels["ol.mit.edu/pod-security-group"] == "myapp-sg"

    return pulumi.Output.all(
        app.dev_shell_deployment.spec,
        app.application_deployment.spec.selector.match_labels,
    ).apply(check)


@pulumi.runtime.test
def test_dev_shell_pod_is_undisruptable_and_runs_app_image():
    app = OLApplicationK8s(
        _base_config(
            application_name="shellpod",
            dev_shell_config=OLApplicationK8sDevShellConfig(
                resource_requests={"cpu": "1", "memory": "8Gi"},
                resource_limits={"memory": "8Gi"},
                termination_grace_period_seconds=120,
            ),
        )
    )
    assert app.dev_shell_deployment is not None

    def check(args):
        template, webapp_image = args
        annotations = template["metadata"]["annotations"]
        assert annotations["karpenter.sh/do-not-disrupt"] == "true"
        assert annotations["kubectl.kubernetes.io/default-container"] == "dev-shell"
        pod = template["spec"]
        assert pod["termination_grace_period_seconds"] == 120
        assert len(pod["containers"]) == 1
        container = pod["containers"][0]
        assert container["name"] == "dev-shell"
        assert container["image"] == webapp_image
        assert container["command"] == ["sleep", "infinity"]
        assert container["resources"]["limits"] == {"memory": "8Gi"}
        assert container["resources"]["requests"] == {"cpu": "1", "memory": "8Gi"}
        assert "ports" not in container or not container["ports"]
        assert "liveness_probe" not in container or container["liveness_probe"] is None
        assert container["env_from"][0]["secret_ref"]["name"] == "myapp-secret"

    return pulumi.Output.all(
        app.dev_shell_deployment.spec.template,
        app.application_deployment.spec.template.spec.containers[0].image,
    ).apply(check)
