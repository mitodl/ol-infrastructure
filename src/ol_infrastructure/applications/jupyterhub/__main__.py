"""JupyterHub application deployment for MIT Open Learning."""

from dataclasses import dataclass

from pulumi import Config, ResourceOptions, StackReference
from pulumi_aws import ec2, get_caller_identity, iam

from bridge.lib.magic_numbers import (
    DEFAULT_POSTGRES_PORT,
)
from ol_infrastructure.applications.jupyterhub.deployment import (
    provision_jupyterhub_deployment,
)
from ol_infrastructure.components.aws.database import OLAmazonDB, OLPostgresDBConfig
from ol_infrastructure.components.aws.s3 import OLBucket, S3BucketConfig
from ol_infrastructure.components.services.vault import (
    OLVaultDatabaseBackend,
    OLVaultPostgresDatabaseConfig,
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
from ol_infrastructure.lib.vault import postgres_role_statements, setup_vault_provider

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
aws_account = get_caller_identity()

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

deployment_configs = jupyterhub_config.require_object("deployments")

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
            "se1",
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
jupyterhub_authoring_db_config = OLPostgresDBConfig(
    instance_name=f"jupyterhub-db-{stack_info.env_suffix}",
    password=rds_password,
    subnet_group_name=target_vpc["rds_subnet"],
    security_groups=[jupyterhub_db_security_group],
    tags=aws_config.tags,
    db_name="jupyterhub_authoring",
    **rds_defaults,
)
jupyterhub_db = OLAmazonDB(jupyterhub_db_config)

app_vault_backend_config = OLVaultPostgresDatabaseConfig(
    db_name=jupyterhub_db_config.db_name,
    mount_point=f"{jupyterhub_db_config.engine}-{jupyterhub_db_config.db_name}",
    db_admin_username=jupyterhub_db_config.username,
    db_admin_password=jupyterhub_db_config.password.get_secret_value(),
    db_host=jupyterhub_db.db_instance.address,
    role_statements=postgres_role_statements,
)

authoring_vault_db_config = OLVaultPostgresDatabaseConfig(
    db_name=jupyterhub_authoring_db_config.db_name,
    mount_point=f"{jupyterhub_authoring_db_config.engine}-{jupyterhub_authoring_db_config.db_name}",
    db_admin_username=jupyterhub_authoring_db_config.username,
    db_admin_password=jupyterhub_authoring_db_config.password.get_secret_value(),
    db_host=jupyterhub_db.db_instance.address,
    role_statements=postgres_role_statements,
)

# Vault Database Backend
app_vault_backend = OLVaultDatabaseBackend(
    app_vault_backend_config,
    opts=ResourceOptions(depends_on=[jupyterhub_db]),
)

# Vault Database Backend
authoring_vault_backend = OLVaultDatabaseBackend(
    authoring_vault_db_config,
    opts=ResourceOptions(depends_on=[jupyterhub_db]),
)


# We may want to rethink this. It's a bit cumbersome
# If we more directly scoped the database to the stack
# instead of naming it based on the original
# deployment we could probably clean
# this abstraction up a bit, but that'd require a teardown.
@dataclass
class JupyterhubDeploymentInfo:
    name: str
    extra_images: dict[str, dict[str, str]]
    vault_backend: OLVaultDatabaseBackend
    db_config: OLPostgresDBConfig


JupyterhubInfo = JupyterhubDeploymentInfo(
    name="jupyterhub",
    extra_images=EXTRA_IMAGES,
    vault_backend=app_vault_backend,
    db_config=jupyterhub_db_config,
)
JupyterhubAuthoringInfo = JupyterhubDeploymentInfo(
    name="jupyterhub-authoring",
    extra_images={},
    vault_backend=authoring_vault_backend,
    db_config=jupyterhub_authoring_db_config,
)


deployment_to_jupyterhub_info = {
    "jupyterhub": JupyterhubInfo,
    "jupyterhub-authoring": JupyterhubAuthoringInfo,
}

# Provision JupyterHub deployments
for deployment_config in deployment_configs:
    namespace = deployment_config["namespace"]
    cluster_stack.require_output("namespaces").apply(
        lambda ns, namespace=namespace: check_cluster_namespace(namespace, ns)
    )
    jupyterhub_deployment = provision_jupyterhub_deployment(
        stack_info=stack_info,
        jupyterhub_deployment_config=deployment_config,
        vault_config=vault_config,
        db_config=jupyterhub_db_config,
        app_vault_backend=deployment_to_jupyterhub_info[
            deployment_config["name"]
        ].vault_backend,
        cluster_stack=cluster_stack,
        application_labels=application_labels,
        k8s_global_labels=k8s_global_labels,
        extra_images=deployment_to_jupyterhub_info[
            deployment_config["name"]
        ].extra_images,
    )
