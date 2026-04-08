import pulumi_kubernetes as kubernetes
from pulumi import Config, ResourceOptions

from bridge.lib.versions import TYPESENSE_VERSION
from ol_infrastructure.lib.pulumi_helper import StackInfo


def create_typesense_resources(
    stack_info: StackInfo,
    namespace: str,
    k8s_global_labels: dict[str, str],
) -> kubernetes.apiextensions.CustomResource | None:
    """Create Typesense resources in Kubernetes.
    Args:
        stack_info (StackInfo): Information about the current stack,
        used for naming and labeling resources.
        namespace (str): The Kubernetes namespace where the Typesense
        resources should be created.
        k8s_global_labels (dict[str, str]): A dictionary of global
        labels to apply to all created Kubernetes resources.
    Returns:
        kubernetes.apiextensions.v1.CustomResource: The created Typesense Custom
        Resource, or None if creation failed / was not called for.
    """

    typesense_config = Config("typesense")
    if not typesense_config.get_bool("deploy"):
        return None

    typesense_bootstrap_key_name = "typesense-bootstrap-key"
    typesense_bootstrap_key = kubernetes.core.v1.Secret(
        f"ol-{stack_info.env_prefix}-edxapp-typesesnse-bootstrap-key-{stack_info.env_suffix}",
        metadata=kubernetes.meta.v1.ObjectMetaArgs(
            name=typesense_bootstrap_key_name,
            namespace=namespace,
            labels=k8s_global_labels,
        ),
        string_data={
            "typesense-api-key": typesense_config.require("bootstrap_key"),
        },
        opts=ResourceOptions(),
    )

    return kubernetes.apiextensions.CustomResource(
        f"ol-{stack_info.env_prefix}-edxapp-typesense-crd-{stack_info.env_suffix}",
        api_version="ts.opentelekomcloud.com/v1alpha1",
        kind="TypesenseCluster",
        metadata=kubernetes.meta.v1.ObjectMetaArgs(
            name=f"{stack_info.env_prefix}-ts",
            namespace=namespace,
            labels=k8s_global_labels,
        ),
        spec={
            "image": typesense_config.get("image")
            or f"typesense/typesense:{TYPESENSE_VERSION}",
            "replicas": typesense_config.get_int("replicas") or 3,
            "resources": {
                "requests": {
                    "cpu": typesense_config.get("cpu_request") or "100m",
                    "memory": typesense_config.get("memory_request") or "512Mi",
                },
                "limits": {
                    "memory": typesense_config.get("memory_limit") or "512Mi",
                },
            },
            "storage": {
                "size": typesense_config.get("storage_size") or "100Gi",
                "storageClassName": typesense_config.get("storage_class_name")
                or "ebs-gp3-sc",
            },
            "adminApiKey": {
                "name": typesense_bootstrap_key_name,
            },
        },
        opts=ResourceOptions(depends_on=[typesense_bootstrap_key]),
    )
