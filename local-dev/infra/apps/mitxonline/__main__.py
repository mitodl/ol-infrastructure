"""MITx Online in the local k3d cluster, from the deployed definition.

Bindings come from the local platform: environment values from the tracked
ConfigMap in local-dev/apps/mitxonline, the developer's gitignored override
file merged over them, and the tracked Secret. The routes stay hand-written
until OLApisixOIDCResources can read its credentials from a plain Secret.
"""

import os
from pathlib import Path
from typing import Any

import pulumi_kubernetes as k8s
import yaml
from pulumi import ResourceOptions, ResourceTransformArgs, ResourceTransformResult

from ol_infrastructure.applications.mitxonline import definition
from ol_infrastructure.components.services.k8s import OLApplicationK8s

ROOT_DOMAIN_DEFAULT = "mit.dev"
APP_DIR = Path(__file__).resolve().parents[3] / "apps" / "mitxonline"
IMAGE_DEFAULT = "mitodl/mitxonline-app:latest"
LABELS = {
    "ol.mit.edu/application": "mitxonline",
    "ol.mit.edu/environment": "local-dev",
    "ol.mit.edu/managed-by": "local-dev-pulumi",
}

root_domain = os.environ.get("LOCAL_DEV_ROOT_DOMAIN", ROOT_DOMAIN_DEFAULT)


def _manifest(path: Path) -> dict[str, Any]:
    text = path.read_text().replace(ROOT_DOMAIN_DEFAULT, root_domain)
    return yaml.safe_load(text)


def _image(reference: str) -> tuple[str, str]:
    """Split an image reference into repository and tag."""
    repository, _, tag = reference.rpartition(":")
    if "/" in tag or not repository:
        return reference, "latest"
    return repository, tag


env_vars: dict[str, Any] = dict(_manifest(APP_DIR / "configmaps/app-env.yaml")["data"])
overrides = APP_DIR / "configmaps/app-env.local.yaml"
if overrides.exists():
    env_vars.update(_manifest(overrides).get("data") or {})

secret_manifest = _manifest(APP_DIR / "secrets.yaml")
secret_name = secret_manifest["metadata"]["name"]

provider = k8s.Provider("k3d-local", context="local-dev")

secret = k8s.core.v1.Secret(
    secret_name,
    metadata=k8s.meta.v1.ObjectMetaArgs(
        name=secret_name, namespace=definition.NAMESPACE, labels=LABELS
    ),
    string_data=secret_manifest["stringData"],
    opts=ResourceOptions(provider=provider),
)

repository, tag = _image(os.environ.get("TILT_IMAGE_0", IMAGE_DEFAULT))

bindings = definition.MitxonlineBindings(
    env_suffix="dev",
    hostnames=definition.MitxonlineHostnames(
        api=f"api.mitxonline.{root_domain}",
        frontend=f"mitxonline.{root_domain}",
        learn_api=f"api.learn.{root_domain}",
    ),
    environment_variables=env_vars,
    k8s_labels=LABELS,
    secret_names=[secret_name],
    application_docker_tag=tag,
    cluster=definition.ClusterCapabilities(
        registry="direct", autoscalers=False, pod_monitors=False
    ),
    min_replicas=1,
    web_memory_limit="1Gi",
    granian_reload=True,
    celery_topology="merged",
)

# The mkcert root, merged into the image's CA bundle, so pods trust both public
# endpoints and local TLS. app-env.yaml points the CA variables at this path.
ca_bundle_init = k8s.core.v1.ContainerArgs(
    name="ca-bundle-merge",
    image=f"{repository}:{tag}",
    command=["/bin/sh", "-c"],
    args=[
        "cat /etc/ssl/certs/ca-certificates.crt /mkcert/rootCA.pem"
        " > /etc/ssl/local-dev/rootCA.pem"
    ],
    volume_mounts=[
        k8s.core.v1.VolumeMountArgs(name="mkcert-root-ca", mount_path="/mkcert")
    ],
)

app_config = definition.application_config(bindings)
app_config = app_config.model_copy(
    update={
        "application_image_repository": repository,
        "extra_init_containers": [ca_bundle_init],
        "extra_volumes": [
            k8s.core.v1.VolumeArgs(
                name="mkcert-root-ca",
                config_map=k8s.core.v1.ConfigMapVolumeSourceArgs(name="mkcert-root-ca"),
            ),
            k8s.core.v1.VolumeArgs(
                name="combined-ca", empty_dir=k8s.core.v1.EmptyDirVolumeSourceArgs()
            ),
        ],
        "extra_volume_mounts": [
            k8s.core.v1.VolumeMountArgs(
                name="combined-ca", mount_path="/etc/ssl/local-dev"
            )
        ],
        "resource_requests": {"cpu": "100m", "memory": "512Mi"},
        "pre_deploy_resource_requests": {"cpu": "100m", "memory": "512Mi"},
        "pre_deploy_resource_limits": {"memory": "2Gi"},
        "celery_worker_configs": [
            worker.model_copy(
                update={
                    "resource_requests": {"cpu": "50m", "memory": "256Mi"},
                    "resource_limits": {"memory": "1Gi"},
                }
            )
            for worker in app_config.celery_worker_configs
        ],
        "application_deployment_use_anti_affinity": False,
    }
)

SKIP_AWAIT_TYPES = {"kubernetes:apps/v1:Deployment", "kubernetes:core/v1:Service"}


def _skip_readiness_await(
    args: ResourceTransformArgs,
) -> ResourceTransformResult | None:
    """Return from pulumi up once a workload is accepted, not once it is ready.

    Pulumi waits for a Deployment's pods and for a Service's endpoints, which is
    pod readiness either way. Tilt tracks rollout itself and times the apply
    command out, so that wait only delays the YAML Tilt needs. The pre-deploy
    Job keeps its wait: the webapp Deployment depends on it, so migrations still
    finish before the webapp rolls.
    """
    if args.type_ not in SKIP_AWAIT_TYPES:
        return None
    metadata = args.props.get("metadata")
    if isinstance(metadata, k8s.meta.v1.ObjectMetaArgs):
        annotations = dict(metadata.annotations or {})
        annotations["pulumi.com/skipAwait"] = "true"
        metadata.annotations = annotations
    else:
        metadata = dict(metadata or {})
        metadata["annotations"] = {
            **(metadata.get("annotations") or {}),
            "pulumi.com/skipAwait": "true",
        }
    return ResourceTransformResult(
        props={**args.props, "metadata": metadata}, opts=args.opts
    )


OLApplicationK8s(
    ol_app_k8s_config=app_config,
    opts=ResourceOptions(
        providers=[provider],
        depends_on=[secret],
        transforms=[_skip_readiness_await],
    ),
)
