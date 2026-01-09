"""Create the resources needed to run a codejail service.  # noqa: D200"""

import os

import pulumi_kubernetes as kubernetes
from pulumi import Config, StackReference

from bridge.lib.magic_numbers import CODEJAIL_SERVICE_PORT
from ol_infrastructure.lib.aws.eks_helper import (
    cached_image_uri,
    check_cluster_namespace,
    setup_k8s_provider,
)
from ol_infrastructure.lib.ol_types import (
    BusinessUnit,
    K8sGlobalLabels,
    Services,
)
from ol_infrastructure.lib.pulumi_helper import parse_stack

stack_info = parse_stack()
codejail_config = Config("codejail")

target_cluster = codejail_config.require("target_cluster")
cluster_stack = StackReference(
    f"infrastructure.aws.eks.{target_cluster}.{stack_info.name}"
)
edxapp_stack = StackReference(
    f"applications.edxapp.{stack_info.env_prefix}.{stack_info.name}"
)

env_name = f"{stack_info.env_prefix}-{stack_info.env_suffix}"

k8s_global_labels = K8sGlobalLabels(
    service=Services.codejail,
    ou=BusinessUnit(codejail_config.require("business_unit")),
    stack=stack_info,
).model_dump()

setup_k8s_provider(kubeconfig=cluster_stack.require_output("kube_config"))

# Figure out the namespace and ensure it exists in the cluster
namespace = f"{stack_info.env_prefix}-openedx"
cluster_stack.require_output("namespaces").apply(
    lambda ns: check_cluster_namespace(namespace, ns)
)

# Ensure that the docker image digest is set in the environment
if "CODEJAIL_DOCKER_IMAGE_DIGEST" not in os.environ:
    msg = "Environment variable CODEJAIL_DOCKER_IMAGE_DIGEST is not set. "
    raise OSError(msg)
CODEJAIL_DOCKER_IMAGE_DIGEST = os.environ["CODEJAIL_DOCKER_IMAGE_DIGEST"]
codejail_image = cached_image_uri(f"mitodl/codejail@{CODEJAIL_DOCKER_IMAGE_DIGEST}")

app_labels = k8s_global_labels | {
    "ol.mit.edu/application": "codejail",
}

codejail_deployment = kubernetes.apps.v1.Deployment(
    f"codejail-deployment-{env_name}",
    metadata=kubernetes.meta.v1.ObjectMetaArgs(
        name="codejail",
        labels=app_labels,
        namespace=namespace,
    ),
    spec=kubernetes.apps.v1.DeploymentSpecArgs(
        replicas=codejail_config.get_int("replicas") or 1,
        selector=kubernetes.meta.v1.LabelSelectorArgs(
            match_labels=app_labels,
        ),
        template=kubernetes.core.v1.PodTemplateSpecArgs(
            metadata=kubernetes.meta.v1.ObjectMetaArgs(
                labels=app_labels,
            ),
            spec=kubernetes.core.v1.PodSpecArgs(
                containers=[
                    kubernetes.core.v1.ContainerArgs(
                        name="codejail",
                        image=codejail_image,
                        resources=kubernetes.core.v1.ResourceRequirementsArgs(
                            requests={
                                "cpu": "100m",
                                "memory": "256Mi",
                            },
                            limits={
                                "memory": "265Mi",
                            },
                        ),
                        env=[
                            kubernetes.core.v1.EnvVarArgs(
                                name="FLASK_CODEJAILSERVICE_HOST",
                                value="0.0.0.0",  # noqa: S104
                            ),
                            kubernetes.core.v1.EnvVarArgs(
                                name="FLASK_CODEJAILSERVICE_PORT",
                                value=str(CODEJAIL_SERVICE_PORT),
                            ),
                        ],
                        ports=[
                            kubernetes.core.v1.ContainerPortArgs(
                                container_port=CODEJAIL_SERVICE_PORT,
                                name="http",
                            ),
                        ],
                    ),
                ],
            ),
        ),
    ),
)

codejail_service = kubernetes.core.v1.Service(
    f"codejail-service-{env_name}",
    metadata=kubernetes.meta.v1.ObjectMetaArgs(
        name="codejail",
        labels=app_labels,
        namespace=namespace,
    ),
    spec=kubernetes.core.v1.ServiceSpecArgs(
        type="ClusterIP",
        selector=app_labels,
        ports=[
            kubernetes.core.v1.ServicePortArgs(
                port=CODEJAIL_SERVICE_PORT,
                target_port=CODEJAIL_SERVICE_PORT,
                name="http",
            ),
        ],
    ),
)
