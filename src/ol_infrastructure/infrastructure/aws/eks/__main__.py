# ruff: noqa: ERA001

import json

import pulumi_aws as aws
import pulumi_eks as eks
import yaml
from pulumi import Config, StackReference, export

from bridge.lib.magic_numbers import IAM_ROLE_NAME_PREFIX_MAX_LENGTH
from ol_infrastructure.lib.aws.iam_helper import IAM_POLICY_VERSION
from ol_infrastructure.lib.ol_types import AWSBase
from ol_infrastructure.lib.pulumi_helper import parse_stack

eks_config = Config("eks")
env_config = Config("environment")

stack_info = parse_stack()

network_stack = StackReference(f"infrastructure.aws.network.{stack_info.name}")
vault_stack = StackReference(f"infrastructure.vault.operations.{stack_info.name}")

business_unit = env_config.require("business_unit") or "operations"
target_vpc = network_stack.require_output(env_config.require("target_vpc"))

pod_ip_blocks = target_vpc["k8s_pod_subnet_cidrs"]
pod_subnet_ids = target_vpc["k8s_pod_subnet_ids"]
service_ip_block = target_vpc["k8s_service_subnet_cidr"]

# Commented out means we let the cluster manages it automatically
default_addons = {
    #    "aws-node" - daemonset
    #    "coredns" - deployment
    #    "kube-proxy" - daemonset
    "aws-ebs-csi-driver": {
        "addon_name": "aws-ebs-csi-driver",
        "addon_version": "v1.33.0-eksbuild.1",
    }
}

cluster_name = f"{stack_info.env_prefix}-{stack_info.env_suffix}"

aws_config = AWSBase(
    tags={
        "OU": env_config.get("business_unit") or "operations",
        "Environment": cluster_name,
        "Owner": "platform-engineering",
    },
)

default_assume_role_policy = {
    "Version": IAM_POLICY_VERSION,
    "Statement": {
        "Effect": "Allow",
        "Action": "sts:AssumeRole",
        "Principal": {"Service": "ec2.amazonaws.com"},
    },
}

administrator_iam_role = aws.iam.Role(
    f"{cluster_name}-eks-admin-role",
    assume_role_policy=json.dumps(default_assume_role_policy),
    name_prefix=f"{cluster_name}-eks-admin-role-"[:IAM_ROLE_NAME_PREFIX_MAX_LENGTH],
    path=f"/ol-infrastructure/eks/{cluster_name}/",
    tags=aws_config.tags,
)


cluster = eks.Cluster(
    f"{cluster_name}-eks-cluster",
    name=cluster_name,
    access_entries={
        "admin": eks.AccessEntryArgs(
            principal_arn=administrator_iam_role.arn,
            access_policies={
                "admin": eks.AccessPolicyAssociationArgs(
                    access_scope=aws.eks.AccessPolicyAssociationAccessScopeArgs(
                        type="cluster",
                    ),
                    policy_arn="arn:aws:eks::aws:cluster-access-policy/AmazonEKSClusterAdminPolicy",
                ),
            },
            kubernetes_groups=["admin"],
        )
    },
    authentication_mode=eks.AuthenticationMode("API"),
    endpoint_public_access=True,
    endpoint_private_access=False,
    create_oidc_provider=True,
    fargate=False,
    ip_family="ipv4",
    kubernetes_service_ip_address_range=service_ip_block,
    tags=aws_config.tags,
    vpc_id=target_vpc["id"],
    skip_default_node_group=True,
    node_associate_public_ip_address=False,
    subnet_ids=pod_subnet_ids,
    enabled_cluster_log_types=[
        "api",
        "audit",
        "authenticator",
    ],
)

# Configure addons
extra_addons = eks_config.get_object("extra_addons") or {}
addons = default_addons | extra_addons
for addon_key, addon_definition in addons.items():
    eks.Addon(
        f"{cluster_name}-eks-addon-{addon_key}",
        cluster=cluster,
        **addon_definition,
    )


# Configure node groups
node_role = aws.iam.Role(
    f"{cluster_name}-eks-node-role",
    assume_role_policy=json.dumps(default_assume_role_policy),
    name_prefix=f"{cluster_name}-eks-node-role-"[:IAM_ROLE_NAME_PREFIX_MAX_LENGTH],
    path=f"/ol-infrastructure/eks/{cluster_name}/",
    tags=aws_config.tags,
)
managed_policy_arns = [
    "arn:aws:iam::aws:policy/AmazonEKSWorkerNodePolicy",
    "arn:aws:iam::aws:policy/AmazonEKS_CNI_Policy",
    "arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryReadOnly",
]
for i, policy in enumerate(managed_policy_arns):
    aws.iam.RolePolicyAttachment(
        f"{cluster_name}-eks-node-role-policy-{i}", policy_arn=policy, role=node_role.id
    )
node_instance_profile = aws.iam.InstanceProfile(
    f"{cluster_name}-eks-node-instanceProfile", role=node_role.name
)
for ng_name, ng_config in eks_config.require_object("nodegroups").items():
    taint_list = []
    for taint_name, taint_config in ng_config["taints"].items():
        taint_list.append(
            aws.eks.NodeGroupTaintArgs(
                key=taint_name,
                value=taint_config["value"] or None,
                effect=taint_config["effect"],
            ),
        )
    eks.ManagedNodeGroup(
        f"{cluster_name}-eks-managednodegroup-{ng_name}",
        capacity_type="ON_DEMAND",
        cluster=cluster,
        instance_types=ng_config["instance_types"],
        labels=ng_config["labels"] or {},
        node_group_name=f"{cluster_name}-managednodegroup-{ng_name}",
        node_role_arn=node_role.arn,
        scaling_config=aws.eks.NodeGroupScalingConfigArgs(
            desired_size=ng_config["scaling"]["desired"] or 2,
            max_size=ng_config["scaling"]["max"] or 3,
            min_size=ng_config["scaling"]["min"] or 1,
        ),
        tags=aws_config.merged_tags(ng_config["tags"] or {}),
        taints=taint_list,
    )

export(
    "cluster_export",
    value={"kube_config": cluster.kubeconfig.apply(lambda cc: yaml.dump(cc))},
)
