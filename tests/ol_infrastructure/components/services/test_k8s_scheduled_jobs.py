"""Tests for the OLApplicationK8s scheduled-job CronJobs."""

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
    OLApplicationK8sScheduledJobConfig,
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


@pulumi.runtime.test
def test_scheduled_job_pod_is_not_disrupted_by_consolidation():
    app = OLApplicationK8s(
        _base_config(
            application_name="jobpod",
            scheduled_jobs=[
                OLApplicationK8sScheduledJobConfig(
                    name="reindex", schedule="30 4 * * 0", command=["true"]
                )
            ],
        )
    )

    def check(annotations):
        assert annotations["karpenter.sh/do-not-disrupt"] == "true"

    return app.scheduled_jobs[
        0
    ].spec.job_template.spec.template.metadata.annotations.apply(check)
