"""JupyterHub Data application deployment for MIT Open Learning Data Platform.

This project deploys a JupyterHub instance to the data EKS cluster. It uses
GenericOAuthenticator against the ol-data-platform Keycloak realm so that
JupyterHub itself holds each user's OIDC access token. KubeSpawner then injects
the token as TRINO_TOKEN into the single-user server environment, enabling
per-user JWT auth against Starburst Galaxy without any credential management
in the notebook itself.
"""

from typing import Any

from pulumi import Config, StackReference
from pulumi_aws import ec2, get_caller_identity, iam

from bridge.lib.magic_numbers import DEFAULT_POSTGRES_PORT
from ol_infrastructure.applications.jupyterhub_data.deployment import (
    provision_jupyterhub_data_deployment,
)
from ol_infrastructure.components.aws.database import OLAmazonDB, OLPostgresDBConfig
from ol_infrastructure.components.aws.eks import OLEKSTrustRole, OLEKSTrustRoleConfig
from ol_infrastructure.lib.aws.eks_helper import (
    check_cluster_namespace,
    setup_k8s_provider,
)
from ol_infrastructure.lib.aws.iam_helper import IAM_POLICY_VERSION, lint_iam_policy
from ol_infrastructure.lib.ol_types import (
    Application,
    AWSBase,
    BusinessUnit,
    K8sGlobalLabels,
    Services,
)
from ol_infrastructure.lib.pulumi_helper import parse_stack
from ol_infrastructure.lib.stack_defaults import defaults
from ol_infrastructure.lib.vault import setup_vault_provider

stack_info = parse_stack()
setup_vault_provider(stack_info)
env_name = f"{stack_info.env_prefix}-{stack_info.env_suffix}"

jupyterhub_data_config = Config("jupyterhub_data")
vault_config = Config("vault")

network_stack = StackReference(f"infrastructure.aws.network.{stack_info.name}")
vault_stack = StackReference(f"infrastructure.vault.operations.{stack_info.name}")
cluster_stack = StackReference(f"infrastructure.aws.eks.data.{stack_info.name}")

data_vpc = network_stack.require_output("data_vpc")
k8s_pod_subnet_cidrs = data_vpc["k8s_pod_subnet_cidrs"]

aws_config = AWSBase(
    tags={"OU": BusinessUnit.data, "Environment": stack_info.env_suffix}
)

k8s_global_labels = K8sGlobalLabels(
    application=Application.jupyterhub,
    service=Services.notebooks,
    ou=BusinessUnit.data,
    stack=stack_info,
).model_dump()

application_labels = k8s_global_labels | {
    "ol.mit.edu/application": "jupyterhub-data",
}

setup_k8s_provider(kubeconfig=cluster_stack.require_output("kube_config"))
aws_account = get_caller_identity()

jupyterhub_data_namespace = "jupyter-data"
cluster_stack.require_output("namespaces").apply(
    lambda ns: check_cluster_namespace(jupyterhub_data_namespace, ns)
)

# IAM policy granting read-only access to OL data lake S3 buckets and Glue catalog.
# Single-user notebook pods use this via IRSA to run PyIceberg and direct S3 queries.
jupyterhub_data_lake_policy_document = {
    "Version": IAM_POLICY_VERSION,
    "Statement": [
        {
            "Effect": "Allow",
            "Action": [
                "s3:GetObject",
                "s3:ListBucket",
            ],
            "Resource": [
                f"arn:aws:s3:::ol-data-lake-*-{stack_info.env_suffix}",
                f"arn:aws:s3:::ol-data-lake-*-{stack_info.env_suffix}/*",
            ],
        },
        {
            "Effect": "Allow",
            "Action": [
                "glue:GetDatabase",
                "glue:GetDatabases",
                "glue:GetPartition",
                "glue:GetPartitions",
                "glue:GetTable",
                "glue:GetTables",
            ],
            "Resource": [
                "arn:aws:glue:us-east-1:610119931565:catalog",
                (
                    "arn:aws:glue:us-east-1:610119931565:database"
                    f"/ol_warehouse_{stack_info.env_suffix}*"
                ),
                (
                    "arn:aws:glue:us-east-1:610119931565:table"
                    f"/ol_warehouse_{stack_info.env_suffix}*/*"
                ),
            ],
        },
    ],
}
parliament_config: dict[str, Any] = {
    "RESOURCE_STAR": {"ignore_locations": []},
}

jupyterhub_data_lake_policy = iam.Policy(
    f"jupyterhub-data-lake-iam-policy-{stack_info.env_suffix}",
    path=(
        f"/ol-applications/jupyterhub-data"
        f"/{stack_info.env_prefix}/{stack_info.env_suffix}/"
    ),
    description=(
        "Read-only access to OL data lake S3 and Glue catalog "
        "for JupyterHub data notebook pods."
    ),
    policy=lint_iam_policy(
        jupyterhub_data_lake_policy_document,
        stringify=True,
        parliament_config=parliament_config,
    ),
    tags=aws_config.tags,
)

jupyterhub_data_service_account_name = "jupyterhub-data-service-account"
jupyterhub_data_trust_role_config = OLEKSTrustRoleConfig(
    account_id=aws_account.account_id,
    cluster_name=f"jupyterhub-data-{stack_info.name}",
    cluster_identities=cluster_stack.require_output("cluster_identities"),
    description=(
        "Trust role for JupyterHub data service account "
        "to read the OL data lake via IRSA"
    ),
    policy_operator="StringEquals",
    role_name="jupyterhub-data",
    service_account_identifier=[
        (
            f"system:serviceaccount:{jupyterhub_data_namespace}"
            f":{jupyterhub_data_service_account_name}"
        ),
    ],
    tags=aws_config.tags,
)

jupyterhub_data_trust_role = OLEKSTrustRole(
    f"jupyterhub-data-ol-trust-role-{stack_info.env_suffix}",
    role_config=jupyterhub_data_trust_role_config,
)
iam.RolePolicyAttachment(
    f"jupyterhub-data-policy-attachment-{stack_info.env_suffix}",
    policy_arn=jupyterhub_data_lake_policy.arn,
    role=jupyterhub_data_trust_role.role.name,
)

# Dedicated RDS instance in the data VPC for JupyterHub session / user state
rds_defaults = defaults(stack_info)["rds"]
rds_defaults["instance_size"] = (
    jupyterhub_data_config.get("db_instance_size") or rds_defaults["instance_size"]
)
rds_defaults["use_blue_green"] = False
rds_defaults["read_replica"] = None
rds_password = jupyterhub_data_config.require("rds_password")

jupyterhub_data_db_security_group = ec2.SecurityGroup(
    f"jupyterhub-data-db-security-group-{env_name}",
    name=f"jupyterhub-data-db-{env_name}",
    description="Access from jupyterhub-data pods and Vault to the JupyterHub data PostgreSQL instance.",  # noqa: E501
    ingress=[
        ec2.SecurityGroupIngressArgs(
            security_groups=[
                vault_stack.require_output("vault_server")["security_group"],
            ],
            cidr_blocks=[data_vpc["cidr"]],
            protocol="tcp",
            from_port=DEFAULT_POSTGRES_PORT,
            to_port=DEFAULT_POSTGRES_PORT,
            description="Vault server access for dynamic credential generation.",
        ),
        ec2.SecurityGroupIngressArgs(
            cidr_blocks=k8s_pod_subnet_cidrs,
            description="Kubernetes pod CIDR blocks for JupyterHub hub and user pods.",
            from_port=DEFAULT_POSTGRES_PORT,
            protocol="tcp",
            security_groups=[],
            to_port=DEFAULT_POSTGRES_PORT,
        ),
    ],
    tags=aws_config.tags,
    vpc_id=data_vpc["id"],
)

jupyterhub_data_db_config = OLPostgresDBConfig(
    instance_name=f"jupyterhub-data-db-{stack_info.env_suffix}",
    password=rds_password,
    subnet_group_name=data_vpc["rds_subnet"],
    security_groups=[jupyterhub_data_db_security_group],
    tags=aws_config.tags,
    db_name="jupyterhub_data",
    **rds_defaults,
)
jupyterhub_data_db = OLAmazonDB(jupyterhub_data_db_config)

domain_name = jupyterhub_data_config.require("domain")
trino_host = jupyterhub_data_config.get("trino_host") or ""

provision_jupyterhub_data_deployment(
    stack_info=stack_info,
    domain_name=domain_name,
    trino_host=trino_host,
    namespace=jupyterhub_data_namespace,
    vault_config=vault_config,
    jupyterhub_data_db=jupyterhub_data_db,
    db_config=jupyterhub_data_db_config,
    cluster_stack=cluster_stack,
    application_labels=application_labels,
    k8s_global_labels=k8s_global_labels,
    service_account_name=jupyterhub_data_service_account_name,
    service_trust_role=jupyterhub_data_trust_role,
    jupyterhub_data_config=jupyterhub_data_config,
)
