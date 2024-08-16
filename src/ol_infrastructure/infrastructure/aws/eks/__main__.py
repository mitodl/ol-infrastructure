import json

import pulumi_aws
import pulumi_eks as eks
import yaml
from bridge.lib.magic_numbers import IAM_ROLE_NAME_PREFIX_MAX_LENGTH
from pulumi import Config, StackReference, export

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

cluster_name = f"{stack_info.env_prefix}-{stack_info.env_suffix}-eks-cluster"

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

administrator_iam_role = pulumi_aws.iam.Role(
    f"{cluster_name}-admin-role",
    assume_role_policy=json.dumps(default_assume_role_policy),
    name_prefix=f"{cluster_name}-admin-role-"[:IAM_ROLE_NAME_PREFIX_MAX_LENGTH],
    path=f"/ol-infrastructure/eks/{cluster_name}/",
    tags=aws_config.tags,
)

node_role = pulumi_aws.iam.Role(
    f"{cluster_name}-node-role",
    assume_role_policy=json.dumps(default_assume_role_policy),
    name_prefix=f"{cluster_name}-node-role-"[:IAM_ROLE_NAME_PREFIX_MAX_LENGTH],
    path=f"/ol-infrastructure/eks/{cluster_name}/",
    tags=aws_config.tags,
)
managed_policy_arns = [
    "arn:aws:iam::aws:policy/AmazonEKSWorkerNodePolicy",
    "arn:aws:iam::aws:policy/AmazonEKS_CNI_Policy",
    "arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryReadOnly",
]
for i, policy in enumerate(managed_policy_arns):
    # Create RolePolicyAttachment without returning it.
    pulumi_aws.iam.RolePolicyAttachment(
        f"{cluster_name}-node-role-policy-{i}", policy_arn=policy, role=node_role.id
    )
node_instance_profile = pulumi_aws.iam.InstanceProfile(
    f"{cluster_name}-node-instanceProfile", role=node_role.name
)

cluster = eks.Cluster(
    cluster_name,
    name=cluster_name,
    access_entries={
        "admin": eks.AccessEntryArgs(
            principal_arn=administrator_iam_role.arn,
            access_policies={
                "admin": eks.AccessPolicyAssociationArgs(
                    access_scope=pulumi_aws.eks.AccessPolicyAssociationAccessScopeArgs(
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


eks.ManagedNodeGroup(
    f"{cluster_name}-managednodegroup-simple",
    cluster=cluster,
    # cluster_name=cluster._name,
    capacity_type="ON_DEMAND",
    instance_types=["t3.medium"],
    node_group_name=f"{cluster_name}-managednodegroup-simple-testing",
    # node_subnet_ids=pod_subnet_ids,
    node_role_arn=node_role.arn,
)

# change this to a managed node group
# eks.NodeGroupV2(
#    f"{cluster_name}-nodegroup-simple",
#    cluster=cluster,
#    instance_type="t3.medium",
#    desired_capacity=2,
#    min_size=1,
#    max_size=2,
#    node_subnet_ids=pod_subnet_ids,
#    instance_profile=node_instance_profile,
# )

export(
    "cluster_export",
    value={"kube_config": cluster.kubeconfig.apply(lambda cc: yaml.dump(cc))},
)
