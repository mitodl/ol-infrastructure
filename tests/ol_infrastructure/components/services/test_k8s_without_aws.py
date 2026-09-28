"""OLApplicationK8s loads and renders on a machine with no AWS configuration.

Runs in a subprocess because the property under test is what happens at
import, and every other test module has already imported the component by the
time this one is collected.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap

PROGRAM = textwrap.dedent(
    """
    import asyncio

    import boto3
    import pulumi

    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())


    def refuse_client(*args, **kwargs):
        raise AssertionError(f"boto3.client called with {args}")


    boto3.client = refuse_client


    class Mocks(pulumi.runtime.Mocks):
        def new_resource(self, args):
            return [args.name + "_id", args.inputs]

        def call(self, args):
            raise AssertionError(f"provider function called: {args.token}")


    pulumi.runtime.set_mocks(Mocks())

    from ol_infrastructure.components.services.k8s import (
        OLApplicationK8s,
        OLApplicationK8sConfig,
    )


    @pulumi.runtime.test
    def render():
        app = OLApplicationK8s(
            OLApplicationK8sConfig(
                application_name="myapp",
                application_namespace="myapp-ns",
                application_image_repository="k3d-registry.localhost:5001/myapp",
                application_docker_tag="tilt-abc123",
                application_lb_service_name="myapp-service",
                application_lb_service_port_name="http",
                application_config={},
                env_from_secret_names=[],
                project_root="/nonexistent",
                import_nginx_config=False,
                k8s_global_labels={"ol.mit.edu/application": "myapp"},
                registry="direct",
                manage_webapp_autoscaler=False,
                manage_celery_autoscalers=False,
                manage_pod_monitor=False,
                manage_webapp_memory_vpa=False,
            )
        )
        return app.application_deployment.spec.template.spec.containers.apply(
            lambda containers: print("image=" + containers[0]["image"])
        )


    render()
    """
)


def test_component_renders_without_aws_configuration():
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("AWS_") and key != "VIRTUAL_ENV"
    }
    env.update(
        {
            "AWS_CONFIG_FILE": os.devnull,
            "AWS_SHARED_CREDENTIALS_FILE": os.devnull,
            "AWS_EC2_METADATA_DISABLED": "true",
        }
    )
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-c", PROGRAM],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "image=k3d-registry.localhost:5001/myapp:tilt-abc123" in result.stdout
