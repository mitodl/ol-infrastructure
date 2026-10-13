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


def _assert_not_selected(selector: dict[str, str], pod_labels: dict[str, str]) -> None:
    assert any(pod_labels.get(key) != value for key, value in selector.items())


@pulumi.runtime.test
def test_deploy_job_pods_are_not_webapp_service_endpoints():
    """A Job pod has no readiness probe, so matching the Service makes it an endpoint.

    Nothing in it listens on the app port, so the connections it is sent are refused
    for as long as the Job runs.
    """
    app = OLApplicationK8s(
        _base_config(
            application_name="jobsel",
            post_deploy_commands=[("warm", ["python", "manage.py", "warm"])],
            # A caller's global labels must not be able to put the Job pods back
            # on the webapp selector.
            k8s_global_labels={
                "ol.mit.edu/environment": "qa",
                "ol.mit.edu/component": "webapp",
                "ol.mit.edu/process": "webapp",
            },
        )
    )
    assert app.pre_deploy_job is not None
    assert app.post_deploy_job is not None

    def check(args):
        service_selector, deployment_selector, pre_template, post_template = args
        for job, template in (
            ("pre-deploy", pre_template),
            ("post-deploy", post_template),
        ):
            pod_labels = template["metadata"]["labels"]
            _assert_not_selected(service_selector, pod_labels)
            _assert_not_selected(deployment_selector["match_labels"], pod_labels)
            assert pod_labels["ol.mit.edu/job"] == job
            # The SecurityGroupPolicy selects on this, and without it the Job has
            # no route to the database it migrates.
            assert pod_labels["ol.mit.edu/pod-security-group"] == "myapp-sg"

    return pulumi.Output.all(
        app.application_service.spec.selector,
        app.application_deployment.spec.selector,
        app.pre_deploy_job.spec.template,
        app.post_deploy_job.spec.template,
    ).apply(check)
