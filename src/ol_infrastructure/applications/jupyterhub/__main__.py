"""JupyterHub application deployment for MIT Open Learning."""

from pulumi import Config, StackReference

from ol_infrastructure.applications.jupyterhub.deployment import (
    provision_jupyterhub_deployment,
)
from ol_infrastructure.lib.aws.eks_helper import (
    check_cluster_namespace,
    setup_k8s_provider,
)
from ol_infrastructure.lib.ol_types import (
    AWSBase,
    BusinessUnit,
    K8sGlobalLabels,
    Services,
)
from ol_infrastructure.lib.pulumi_helper import parse_stack
from ol_infrastructure.lib.stack_defaults import defaults
from ol_infrastructure.lib.vault import setup_vault_provider

# Parse stack and setup providers
stack_info = parse_stack()
setup_vault_provider(stack_info)

# Configuration
jupyterhub_config = Config("jupyterhub")
vault_config = Config("vault")

# Stack references
network_stack = StackReference(f"infrastructure.aws.network.{stack_info.name}")
vault_stack = StackReference(f"infrastructure.vault.operations.{stack_info.name}")
cluster_stack = StackReference(f"infrastructure.aws.eks.applications.{stack_info.name}")

# AWS configuration
aws_config = AWSBase(
    tags={"OU": BusinessUnit.mit_learn, "Environment": stack_info.env_suffix}
)

# Kubernetes labels
k8s_global_labels = K8sGlobalLabels(
    service=Services.jupyterhub,
    ou=BusinessUnit.mit_learn,
    stack=stack_info,
).model_dump()

application_labels = k8s_global_labels | {
    "ol.mit.edu/application": "jupyterhub",
}

# Setup Kubernetes provider
setup_k8s_provider(kubeconfig=cluster_stack.require_output("kube_config"))

# Check namespaces
namespace = "jupyter"
cluster_stack.require_output("namespaces").apply(
    lambda ns: check_cluster_namespace(namespace, ns)
)

authoring_namespace = "jupyter-authoring"
cluster_stack.require_output("namespaces").apply(
    lambda ns: check_cluster_namespace(authoring_namespace, ns)
)

# RDS defaults
rds_defaults = defaults(stack_info)["rds"]
rds_defaults["instance_size"] = (
    jupyterhub_config.get("db_instance_size") or rds_defaults["instance_size"]
)
rds_defaults["use_blue_green"] = False

# Extra images for pre-pulling
COURSE_NAMES = [
    "clustering_and_descriptive_ai",
    "deep_learning_foundations_and_applications",
    "supervised_learning_fundamentals",
    "introduction_to_data_analytics_and_machine_learning",
]
COURSE_NAMES.extend(
    [
        f"uai_source-uai.{i}"
        for i in [
            "intro",
            0,
            "0a",
            1,
            2,
            3,
            4,
            5,
            6,
            7,
            8,
            9,
            11,
            12,
            13,
            "st1",
            "mltl1",
            "pm1",
        ]
    ]
)
EXTRA_IMAGES = {
    course_name.replace(".", "-").replace("_", "-"): {
        "name": "610119931565.dkr.ecr.us-east-1.amazonaws.com/ol-course-notebooks",
        "tag": course_name,
    }
    for course_name in COURSE_NAMES
}

# Provision main JupyterHub deployment
jupyterhub_deployment = provision_jupyterhub_deployment(
    base_name="jupyterhub",
    domain_name=jupyterhub_config.require("domain"),
    namespace=namespace,
    stack_info=stack_info,
    jupyterhub_config=jupyterhub_config,
    vault_config=vault_config,
    network_stack=network_stack,
    vault_stack=vault_stack,
    cluster_stack=cluster_stack,
    aws_config=aws_config,
    application_labels=application_labels,
    k8s_global_labels=k8s_global_labels,
    extra_images=EXTRA_IMAGES,
)

# Provision JupyterHub authoring deployment
jupyterhub_authoring_deployment = provision_jupyterhub_deployment(
    base_name="jupyterhub-authoring",
    domain_name=jupyterhub_config.require("authoring_domain"),
    namespace=authoring_namespace,
    stack_info=stack_info,
    jupyterhub_config=jupyterhub_config,
    vault_config=vault_config,
    network_stack=network_stack,
    vault_stack=vault_stack,
    cluster_stack=cluster_stack,
    aws_config=aws_config,
    application_labels=application_labels,
    k8s_global_labels=k8s_global_labels,
    extra_images=EXTRA_IMAGES,
    menu_override_file="author_menu_override.json",
    disabled_extensions_file="author_disabled_extensions.json",
)
