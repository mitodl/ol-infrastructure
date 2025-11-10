"""JupyterHub application deployment for MIT Open Learning."""

from pulumi import Config, StackReference

import pulumi_kubernetes as kubernetes
import pulumi_vault as vault
from pulumi import Config, InvokeOptions, ResourceOptions, StackReference
from pulumi_aws import ec2, get_caller_identity, iam

from bridge.lib.magic_numbers import (
    DEFAULT_POSTGRES_PORT,
)
from ol_infrastructure.components.aws.database import OLAmazonDB, OLPostgresDBConfig
from ol_infrastructure.components.aws.s3 import OLBucket, S3BucketConfig
from ol_infrastructure.components.services.cert_manager import (
    OLCertManagerCert,
    OLCertManagerCertConfig,
)
from ol_infrastructure.components.services.k8s import (
    OLApisixOIDCConfig,
    OLApisixOIDCResources,
    OLApisixRoute,
    OLApisixRouteConfig,
    OLApisixSharedPlugins,
    OLApisixSharedPluginsConfig,
)
from ol_infrastructure.components.services.vault import (
    OLVaultDatabaseBackend,
    OLVaultK8SDynamicSecretConfig,
    OLVaultK8SResources,
    OLVaultK8SResourcesConfig,
    OLVaultK8SSecret,
    OLVaultPostgresDatabaseConfig,
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

# S3 bucket for storing image assets.
# We still host actual images in ECR, but we store assets to build those
# In S3 and Github.
jupyterhub_course_bucket_name = f"jupyter-courses-{stack_info.env_suffix}"
jupyter_course_bucket_config = S3BucketConfig(
    bucket_name=jupyterhub_course_bucket_name,
    versioning_enabled=True,
    bucket_policy_document=iam.get_policy_document(
        statements=[
            iam.GetPolicyDocumentStatementArgs(
                effect="Allow",
                principals=[
                    iam.GetPolicyDocumentStatementPrincipalArgs(
                        type="AWS",
                        identifiers=[f"arn:aws:iam::{aws_account.account_id}:root"],
                    )
                ],
                actions=["s3:*"],
                resources=[
                    f"arn:aws:s3:::{jupyterhub_course_bucket_name}/*",
                    f"arn:aws:s3:::{jupyterhub_course_bucket_name}",
                ],
            )
        ]
    ).json,
    tags=aws_config.tags,
    region=aws_config.region,
)
jupyter_course_bucket = OLBucket(
    f"jupyter-course-bucket-{env_name}", config=jupyter_course_bucket_config
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
    "base_authoring_image",
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
