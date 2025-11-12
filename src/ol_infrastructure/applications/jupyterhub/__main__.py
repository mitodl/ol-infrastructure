"""JupyterHub application deployment for MIT Open Learning."""

from pathlib import Path

from pulumi import Config, StackReference
from pulumi_aws import ec2

from bridge.lib.magic_numbers import DEFAULT_POSTGRES_PORT
from ol_infrastructure.applications.jupyterhub.deployment import (
    provision_jupyterhub_deployment,
)
from ol_infrastructure.components.aws.database import OLAmazonDB, OLPostgresDBConfig
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
env_name = f"{stack_info.env_prefix}-{stack_info.env_suffix}"

# Configuration
jupyterhub_config = Config("jupyterhub")
vault_config = Config("vault")


# Stack references
network_stack = StackReference(f"infrastructure.aws.network.{stack_info.name}")
vault_stack = StackReference(f"infrastructure.vault.operations.{stack_info.name}")
cluster_stack = StackReference(f"infrastructure.aws.eks.applications.{stack_info.name}")

# AWS configuration
apps_vpc = network_stack.require_output("applications_vpc")
k8s_pod_subnet_cidrs = apps_vpc["k8s_pod_subnet_cidrs"]
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
authoring_namespace = "jupyter-authoring"
cluster_stack.require_output("namespaces").apply(
    lambda ns: check_cluster_namespace(namespace, ns)
)
cluster_stack.require_output("namespaces").apply(
    lambda ns: check_cluster_namespace(authoring_namespace, ns)
)

# RDS defaults
rds_defaults = defaults(stack_info)["rds"]
rds_defaults["instance_size"] = (
    jupyterhub_config.get("db_instance_size") or rds_defaults["instance_size"]
)
rds_defaults["use_blue_green"] = False
rds_password = jupyterhub_config.require("rds_password")

target_vpc_name = jupyterhub_config.get("target_vpc") or f"{stack_info.env_prefix}_vpc"
target_vpc = network_stack.require_output(target_vpc_name)
target_vpc_id = target_vpc["id"]

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

#### Database setup ####
# The physical database for Jupyterhub is shared across both the main and authoring
# deployments, but we create separate Vault backends for each to manage credentials
# and roles separately.
jupyterhub_db_security_group = ec2.SecurityGroup(
    f"jupyterhub-db-security-group-{env_name}",
    name=f"jupyterhub-db-{target_vpc_name}-{env_name}",
    description="Access from jupyterhub to its own postgres database.",
    ingress=[
        ec2.SecurityGroupIngressArgs(
            security_groups=[
                vault_stack.require_output("vault_server")["security_group"],
            ],
            cidr_blocks=[target_vpc["cidr"]],
            protocol="tcp",
            from_port=DEFAULT_POSTGRES_PORT,
            to_port=DEFAULT_POSTGRES_PORT,
            description="Access to Postgres from jupyterhub nodes.",
        ),
        ec2.SecurityGroupIngressArgs(
            cidr_blocks=k8s_pod_subnet_cidrs,
            description="Allow k8s cluster ipblocks to talk to DB",
            from_port=DEFAULT_POSTGRES_PORT,
            protocol="tcp",
            security_groups=[],
            to_port=DEFAULT_POSTGRES_PORT,
        ),
    ],
    tags=aws_config.tags,
    vpc_id=target_vpc_id,
)

jupyterhub_db_config = OLPostgresDBConfig(
    instance_name=f"jupyterhub-db-{stack_info.env_suffix}",
    password=rds_password,
    subnet_group_name=target_vpc["rds_subnet"],
    security_groups=[jupyterhub_db_security_group],
    tags=aws_config.tags,
    db_name="jupyterhub",
    **rds_defaults,
)
jupyterhub_db = OLAmazonDB(jupyterhub_db_config)

jupyterhub_authoring_db_config = OLPostgresDBConfig(
    instance_name=f"jupyterhub-authoring-db-{stack_info.env_suffix}",
    password=rds_password,
    subnet_group_name=target_vpc["rds_subnet"],
    security_groups=[jupyterhub_db_security_group],
    tags=aws_config.tags,
    db_name="jupyterhub_authoring",
    **rds_defaults,
)

menu_override = Path(__file__).parent.joinpath("menu_override.json").read_text()
disabled_extensions = (
    Path(__file__).parent.joinpath("disabled_extensions.json").read_text()
)
dynamic_image_config = (
    Path(__file__).parent.joinpath("dynamicImageConfig.py").read_text()
)
# Provision main JupyterHub deployment
jupyterhub_deployment = provision_jupyterhub_deployment(
    base_name="jupyterhub",
    domain_name=jupyterhub_config.require("domain"),
    namespace=namespace,
    stack_info=stack_info,
    jupyterhub_config=jupyterhub_config,
    vault_config=vault_config,
    db_config=jupyterhub_db_config,
    app_db=jupyterhub_db,
    cluster_stack=cluster_stack,
    application_labels=application_labels,
    k8s_global_labels=k8s_global_labels,
    extra_images=EXTRA_IMAGES,
    menu_override_json=menu_override,
    disabled_extensions_json=disabled_extensions,
    extra_config=dynamic_image_config,
)

author_menu_override = (
    Path(__file__).parent.joinpath("author_menu_override.json").read_text()
)
author_disabled_extensions = (
    Path(__file__).parent.joinpath("author_disabled_extensions.json").read_text()
)
# Provision JupyterHub authoring deployment
jupyterhub_authoring_deployment = provision_jupyterhub_deployment(
    base_name="jupyterhub-authoring",
    domain_name=jupyterhub_config.require("authoring_domain"),
    namespace=authoring_namespace,
    stack_info=stack_info,
    jupyterhub_config=jupyterhub_config,
    vault_config=vault_config,
    db_config=jupyterhub_authoring_db_config,
    app_db=jupyterhub_db,
    cluster_stack=cluster_stack,
    application_labels=application_labels,
    k8s_global_labels=k8s_global_labels,
    extra_images=EXTRA_IMAGES,
    menu_override_json=author_menu_override,
    disabled_extensions_json=author_disabled_extensions,
    extra_config=dynamic_image_config,
)
