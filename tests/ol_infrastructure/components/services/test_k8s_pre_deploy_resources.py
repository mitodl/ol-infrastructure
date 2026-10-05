"""Tests for the OLApplicationK8s pre-deploy Job's resources.

The Job gates the webapp rollout, so it must be sizable apart from the webapp:
an app that lowers its serving limit below what its migrations need would
otherwise OOMKill the Job and block every deploy. When no override is given the
Job keeps inheriting the webapp's resources.
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
    OLApplicationK8sConfig,
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
        "resource_requests": {"cpu": "250m", "memory": "1Gi"},
        "resource_limits": {"memory": "1Gi"},
        "pre_deploy_commands": [("migrate", ["python", "manage.py", "migrate"])],
    }
    defaults.update(overrides)
    return OLApplicationK8sConfig(**defaults)


def _job_resources(app: OLApplicationK8s) -> pulumi.Output:
    assert app.pre_deploy_job is not None
    return app.pre_deploy_job.spec.template.apply(
        lambda template: template["spec"]["containers"][0]["resources"]
    )


def test_no_pre_deploy_job_without_commands():
    app = OLApplicationK8s(
        _base_config(application_name="nojob", pre_deploy_commands=None)
    )
    assert app.pre_deploy_job is None


@pulumi.runtime.test
def test_pre_deploy_job_inherits_webapp_resources_by_default():
    app = OLApplicationK8s(_base_config(application_name="inherit"))

    def check(resources):
        assert resources["requests"] == {"cpu": "250m", "memory": "1Gi"}
        assert resources["limits"] == {"memory": "1Gi"}

    return _job_resources(app).apply(check)


@pulumi.runtime.test
def test_pre_deploy_job_uses_its_own_resources_when_set():
    app = OLApplicationK8s(
        _base_config(
            application_name="override",
            pre_deploy_resource_requests={"cpu": "250m", "memory": "3Gi"},
            pre_deploy_resource_limits={"memory": "3Gi"},
        )
    )

    def check(args):
        job_resources, webapp_template = args
        assert job_resources["requests"] == {"cpu": "250m", "memory": "3Gi"}
        assert job_resources["limits"] == {"memory": "3Gi"}
        # The webapp itself keeps its own, smaller, sizing.
        webapp = next(
            c
            for c in webapp_template["spec"]["containers"]
            if c["name"] == "override-app"
        )
        assert webapp["resources"]["limits"] == {"memory": "1Gi"}

    return pulumi.Output.all(
        _job_resources(app),
        app.application_deployment.spec.template,
    ).apply(check)
