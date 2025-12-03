# ruff: noqa: ERA001, E501
"""Pulumi program for deploying an EKS cluster."""

# Misc Ref: https://docs.aws.amazon.com/eks/latest/userguide/associate-service-account-role.html

import json
import os

import boto3
import pulumi_aws as aws
import pulumi_eks as eks
import pulumi_kubernetes as kubernetes
import pulumi_vault as vault
from botocore.exceptions import ClientError
from pulumi import (
    Alias,
    Config,
    Output,
    ResourceOptions,
    StackReference,
    export,
)

from bridge.lib.magic_numbers import (
    DEFAULT_EFS_PORT,
    IAM_ROLE_NAME_PREFIX_MAX_LENGTH,
)
from bridge.lib.versions import (
    APISIX_CHART_VERSION,
    AWS_LOAD_BALANCER_CONTROLLER_CHART_VERSION,
    AWS_NODE_TERMINATION_HANDLER_CHART_VERSION,
    CERT_MANAGER_CHART_VERSION,
    EBS_CSI_DRIVER_VERSION,
    EFS_CSI_DRIVER_VERSION,
    EXTERNAL_DNS_CHART_VERSION,
    GATEWAY_API_VERSION,
    PROMETHEUS_OPERATOR_CRD_VERSION,
    TRAEFIK_CHART_VERSION,
    VAULT_SECRETS_OPERATOR_CHART_VERSION,
)
from ol_infrastructure.components.aws.eks import OLEKSTrustRole, OLEKSTrustRoleConfig
from ol_infrastructure.infrastructure.aws.eks.apisix_official import (
    setup_apisix,
)
from ol_infrastructure.infrastructure.aws.eks.aws_utils import setup_aws_integrations
from ol_infrastructure.infrastructure.aws.eks.cert_manager import setup_cert_manager
from ol_infrastructure.infrastructure.aws.eks.core_dns import create_core_dns_resources
from ol_infrastructure.infrastructure.aws.eks.external_dns import setup_external_dns
from ol_infrastructure.infrastructure.aws.eks.traefik import setup_traefik
from ol_infrastructure.infrastructure.aws.eks.vault_secrets_operator import (
    setup_vault_secrets_operator,
)
from ol_infrastructure.lib.aws.eks_helper import (
    access_entry_opts,
    get_cluster_version,
    get_eks_addon_version,
)
from ol_infrastructure.lib.aws.iam_helper import (
    EKS_ADMIN_USERNAMES,
    IAM_POLICY_VERSION,
    lint_iam_policy,
)
from ol_infrastructure.lib.ol_types import AWSBase
from ol_infrastructure.lib.pulumi_helper import parse_stack
from ol_infrastructure.lib.vault import setup_vault_provider

############################################################
# Configuration defining / loading and prep work
############################################################


eks_config = Config("eks")
env_config = Config("environment")

stack_info = parse_stack()
setup_vault_provider(stack_info)
aws_account = aws.get_caller_identity()

dns_stack = StackReference("infrastructure.aws.dns")
iam_stack = StackReference("infrastructure.aws.iam")
kms_stack = StackReference(f"infrastructure.aws.kms.{stack_info.name}")
network_stack = StackReference(f"infrastructure.aws.network.{stack_info.name}")
policy_stack = StackReference("infrastructure.aws.policies")
vault_stack = StackReference(f"infrastructure.vault.operations.{stack_info.name}")
vault_auth_stack = StackReference("substructure.vault.auth.operations.Production")
concourse_stack = StackReference("applications.concourse.Production")

business_unit = env_config.require("business_unit") or "operations"
target_vpc = network_stack.require_output(env_config.require("target_vpc"))

pod_ip_blocks = target_vpc["k8s_pod_subnet_cidrs"]
public_ip_blocks = target_vpc["k8s_public_subnet_cidrs"]
pod_subnet_ids = target_vpc["k8s_pod_subnet_ids"]
service_ip_block = target_vpc["k8s_service_subnet_cidr"]

cluster_name = f"{stack_info.env_prefix}-{stack_info.env_suffix}"

kms_ebs = kms_stack.require_output("kms_ec2_ebs_key")

namespaces = eks_config.get_object("namespaces") or []

# Centralize version numbers
VERSIONS = {
    "APISIX_CHART": os.environ.get("APISIX_CHART", APISIX_CHART_VERSION),
    "AWS_LOAD_BALANCER_CONTROLLER_CHART": os.environ.get(
        "AWS_LOAD_BALANCER_CONTROLLER_CHART", AWS_LOAD_BALANCER_CONTROLLER_CHART_VERSION
    ),
    "AWS_NODE_TERMINATION_HANDLER_CHART": os.environ.get(
        "AWS_NODE_TERMINATION_HANDLER_CHART", AWS_NODE_TERMINATION_HANDLER_CHART_VERSION
    ),
    "CERT_MANAGER_CHART": os.environ.get(
        "CERT_MANAGER_CHART", CERT_MANAGER_CHART_VERSION
    ),
    "EBS_CSI_DRIVER": os.environ.get("EBS_CSI_DRIVER", EBS_CSI_DRIVER_VERSION),
    "EFS_CSI_DRIVER": os.environ.get("EFS_CSI_DRIVER", EFS_CSI_DRIVER_VERSION),
    "GATEWAY_API": os.environ.get("GATEWAY_API", GATEWAY_API_VERSION),
    "EXTERNAL_DNS_CHART": os.environ.get(
        "EXTERNAL_DNS_CHART", EXTERNAL_DNS_CHART_VERSION
    ),
    "TRAEFIK_CHART": os.environ.get("TRAEFIK_CHART", TRAEFIK_CHART_VERSION),
    "VAULT_SECRETS_OPERATOR_CHART": os.environ.get(
        "VAULT_SECRETS_OPERATOR_CHART", VAULT_SECRETS_OPERATOR_CHART_VERSION
    ),
    # K8S version is special, retrieve it from the AWS APIs
    "KUBERNETES": os.environ.get("KUBERNETES", get_cluster_version()),
    "PROMETHEUS_OPERATOR": os.environ.get(
        "PROMETHEUS_OPERATOR", PROMETHEUS_OPERATOR_CRD_VERSION
    ),
}

# A global toleration to allow operators to run on nodes tainted as
# 'operations' if there are any present in the cluster
operations_tolerations = [
    {
        "key": "operations",
        "operator": "Equal",
        "value": "true",
        "effect": "NoSchedule",
    },
]

aws_config = AWSBase(
    tags={
        "OU": env_config.get("business_unit") or "operations",
        "Environment": cluster_name,
        "Owner": "platform-engineering",
    },
)

default_assume_role_policy = {
    "Version": IAM_POLICY_VERSION,
    "Statement": [
        {
            "Effect": "Allow",
            "Principal": {"Service": ["ec2.amazonaws.com", "eks.amazonaws.com"]},
            "Action": "sts:AssumeRole",
        }
    ],
}
############################################################
# create core IAM resources
############################################################
# IAM role that admins will assume when using kubectl

# This is what will let DevOps access the cluster with kubectl and the
# script in this directory that builds a kube_config file
# This also lets concourse be a cluster administrator
#
# We hook adminsitrators directly by username ARNs
admin_assume_role_policy_document = concourse_stack.require_output(
    "infra-instance-role-arn"
).apply(
    lambda arn: json.dumps(
        {
            "Version": IAM_POLICY_VERSION,
            "Statement": [
                {
                    "Effect": "Allow",
                    "Action": "sts:AssumeRole",
                    "Principal": {
                        "Service": "ec2.amazonaws.com",
                        "AWS": [
                            f"arn:aws:iam::{aws_account.account_id}:user/{username}"
                            for username in EKS_ADMIN_USERNAMES
                        ]
                        + [arn],
                    },
                }
            ],
        }
    )
)
administrator_role = aws.iam.Role(
    f"{cluster_name}-eks-admin-role",
    assume_role_policy=admin_assume_role_policy_document,
    name_prefix=f"{cluster_name}-eks-admin-role-"[:IAM_ROLE_NAME_PREFIX_MAX_LENGTH],
    path=f"/ol-infrastructure/eks/{cluster_name}/",
    tags=aws_config.tags,
)

# Depending on the environment, developers may have different access.
developer_role_policy_name = (
    eks_config.get("developer_role_policy_name") or "AmazonEKSViewPolicy"
)
developer_role_kubernetes_groups = eks_config.get_object(
    "developer_role_kubernetes_groups"
) or ["view"]
developer_role_scope = eks_config.get("developer_role_scope") or "namespace"

access_entries = {
    # This is the access entry for the assume role that devops uses with kubectl
    # Devops is always a cluster administrator
    "admin": eks.AccessEntryArgs(
        principal_arn=administrator_role.arn,
        access_policies={
            "admin": eks.AccessPolicyAssociationArgs(
                access_scope=aws.eks.AccessPolicyAssociationAccessScopeArgs(
                    type="cluster",
                ),
                policy_arn="arn:aws:eks::aws:cluster-access-policy/AmazonEKSClusterAdminPolicy",
            ),
        },
        kubernetes_groups=["admin"],
    ),
    # Note: Node role access entry is automatically created by EKS for self-managed
    # node groups when authentication_mode="API". No explicit creation needed.
}

# Couple ways developers may be given access to the cluster via different scopes.
# Cluster means we give access to the whole cluster and all namespaces
# namespace means we give access only to sepecific resources in the cluster in specific
# namespaces.
if developer_role_scope == "cluster":
    access_entries["developer"] = eks.AccessEntryArgs(
        principal_arn=vault_auth_stack.require_output("eks_shared_developer_role_arn"),
        access_policies={
            "developer": eks.AccessPolicyAssociationArgs(
                access_scope=aws.eks.AccessPolicyAssociationAccessScopeArgs(
                    type="cluster"
                ),
                policy_arn=f"arn:aws:eks::aws:cluster-access-policy/{developer_role_policy_name}",
            ),
        },
        kubernetes_groups=developer_role_kubernetes_groups,
    )

elif developer_role_scope == "namespace":
    access_entries["developer"] = eks.AccessEntryArgs(
        principal_arn=vault_auth_stack.require_output("eks_shared_developer_role_arn"),
        access_policies={
            "developer": eks.AccessPolicyAssociationArgs(
                access_scope=aws.eks.AccessPolicyAssociationAccessScopeArgs(
                    type="namespace",
                    namespaces=[*namespaces, "default", "operations", "kube-system"],
                ),
                policy_arn=f"arn:aws:eks::aws:cluster-access-policy/{developer_role_policy_name}",
            ),
        },
        kubernetes_groups=developer_role_kubernetes_groups,
    )
else:
    msg = f"developer_role_scope = {developer_role_scope} is not a valid value. Must be 'cluster' or 'namespace'."
    raise ValueError(msg)

# These are the access entries for devops users themselves, which allows the
# EKS views in the aws console to work and be useful rather than just errors
for username in EKS_ADMIN_USERNAMES:
    access_entries[username] = eks.AccessEntryArgs(
        principal_arn=f"arn:aws:iam::{aws_account.account_id}:user/{username}",
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

# Cluster role
cluster_role = aws.iam.Role(
    f"{cluster_name}-eks-cluster-role",
    name_prefix=f"{cluster_name}-eks-cluster-role-"[:IAM_ROLE_NAME_PREFIX_MAX_LENGTH],
    assume_role_policy=json.dumps(default_assume_role_policy),
    path=f"/ol-infrastructure/eks/{cluster_name}/",
    tags=aws_config.tags,
)
cluster_policy_arns = [
    "arn:aws:iam::aws:policy/AmazonEKSClusterPolicy",
    "arn:aws:iam::aws:policy/AmazonEKSVPCResourceController",
]
for index, policy in enumerate(cluster_policy_arns):
    aws.iam.RolePolicyAttachment(
        f"{cluster_name}-eks-cluster-role-policy-attachment-{index}",
        policy_arn=policy,
        role=cluster_role.id,
        opts=ResourceOptions(parent=cluster_role),
    )
############################################################
# Provision the cluster
############################################################

# We are going to create a special aws provider to use for cluster and
# any aws.* children it creates. This provider is going to masquerade
# as a global, shared cluster creator role.
# This keeps the cluster from being 'owned' by a specific person.
cluster_creation_aws_provider = aws.Provider(
    "cluster-creation-aws-provider",
    assume_roles=[
        aws.ProviderAssumeRoleArgs(
            role_arn=iam_stack.require_output("eks_cluster_creator_role_arn")
        )
    ],
)
# Actually make the cluster
cluster = eks.Cluster(
    f"{cluster_name}-eks-cluster",
    name=cluster_name,
    access_entries=access_entries,
    authentication_mode=eks.AuthenticationMode("API"),
    create_oidc_provider=True,
    enabled_cluster_log_types=[
        "api",
        "audit",
        "authenticator",
    ],
    endpoint_private_access=False,
    endpoint_public_access=True,
    fargate=False,
    ip_family="ipv4",
    kubernetes_service_ip_address_range=service_ip_block,
    provider_credential_opts=eks.KubeconfigOptionsArgs(role_arn=administrator_role.arn),
    service_role=cluster_role,
    skip_default_node_group=True,
    # node_subnet_ids=target_vpc["k8s_pod_subnet_ids"],
    private_subnet_ids=target_vpc["k8s_pod_subnet_ids"],
    public_subnet_ids=target_vpc["k8s_public_subnet_ids"],
    node_associate_public_ip_address=False,
    tags=aws_config.tags,
    use_default_vpc_cni=False,
    version=VERSIONS["KUBERNETES"],
    # Ref: https://docs.aws.amazon.com/eks/latest/userguide/security-groups-pods-deployment.html
    # Ref: https://docs.aws.amazon.com/eks/latest/userguide/sg-pods-example-deployment.html
    # Ref: https://github.com/aws/amazon-vpc-cni-k8s/blob/master/README.md
    vpc_cni_options=eks.cluster.VpcCniOptionsArgs(
        cni_external_snat=False,
        configuration_values={"env": {"POD_SECURITY_GROUP_ENFORCING_MODE": "standard"}},
        custom_network_config=False,
        disable_tcp_early_demux=True,
        enable_network_policy=False,
        enable_pod_eni=True,
        enable_prefix_delegation=True,
        external_snat=False,
        log_level="DEBUG",
    ),
    vpc_id=target_vpc["id"],
    opts=ResourceOptions(
        provider=cluster_creation_aws_provider,
        parent=cluster_role,
        depends_on=[cluster_role, administrator_role],
    ),
)


def __create_apiserver_security_group_rules(pod_subnet_cidrs):
    for index, subnet_cidr in enumerate(pod_subnet_cidrs):
        aws.vpc.SecurityGroupIngressRule(
            f"{cluster_name}-eks-apiserver-443-sg-rule-{index}",
            security_group_id=cluster.cluster_security_group_id,
            cidr_ipv4=subnet_cidr,
            from_port=443,
            to_port=443,
            ip_protocol="tcp",
            opts=ResourceOptions(
                aliases=[Alias(name=f"{cluster_name}-eks-apiserver-sg-rule-{index}")]
            ),
        )


pod_ip_blocks.apply(
    lambda pod_subnet_cidrs: __create_apiserver_security_group_rules(pod_subnet_cidrs)
)

export("cluster_name", cluster_name)
export("kube_config", cluster.kubeconfig)
export("cluster_identities", cluster.eks_cluster.identities)
export("cluster_version", cluster.eks_cluster.version)
export("admin_role_arn", administrator_role.arn)
export("cluster_security_group_id", cluster.cluster_security_group_id)
export("node_security_group_id", cluster.node_security_group_id)
export("pod_subnet_ids", pod_subnet_ids)
export(
    "kube_config_data",
    {
        "admin_role_arn": administrator_role.arn,
        "certificate-authority-data": cluster.eks_cluster.certificate_authority,
        "server": cluster.eks_cluster.endpoint,
    },
)

cluster_certificate_authority = cluster.eks_cluster.certificate_authority
cluster_address = cluster.eks_cluster.endpoint

Output.all(ca=cluster_certificate_authority, address=cluster_address).apply(
    lambda cluster_atts: vault.generic.Secret(
        f"{cluster_name}-eks-kubeconfig-vault-secret",
        path=f"secret-global/eks/kubeconfigs/{cluster_name}",
        data_json=json.dumps(
            {
                "ca": cluster_atts["ca"]["data"],
                "server": cluster_atts["address"],
            }
        ),
        opts=ResourceOptions(depends_on=[cluster]),
    )
)

############################################################
# Configure node groups
############################################################
# At least one node group must be defined.

# create a node role / instance profile used by all nodes in the cluster
# regardless of what node group they are in
#
# Attached policies depend on configuration flags
node_role = aws.iam.Role(
    f"{cluster_name}-eks-node-role",
    assume_role_policy=json.dumps(default_assume_role_policy),
    name_prefix=f"{cluster_name}-eks-node-role-"[:IAM_ROLE_NAME_PREFIX_MAX_LENGTH],
    path=f"/ol-infrastructure/eks/{cluster_name}/",
    tags=aws_config.tags,
)
managed_node_policy_arns = [
    "arn:aws:iam::aws:policy/AmazonEKSWorkerNodePolicy",
    "arn:aws:iam::aws:policy/AmazonEKS_CNI_Policy",
    "arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryReadOnly",
    policy_stack.require_output("iam_policies")["describe_instances"],
]
if eks_config.get_bool("ebs_csi_provisioner"):
    managed_node_policy_arns.append(
        "arn:aws:iam::aws:policy/service-role/AmazonEBSCSIDriverPolicy"
    )
if eks_config.get_bool("efs_csi_provisioner"):
    managed_node_policy_arns.append(
        "arn:aws:iam::aws:policy/service-role/AmazonEFSCSIDriverPolicy"
    )
for i, policy in enumerate(managed_node_policy_arns):
    aws.iam.RolePolicyAttachment(
        f"{cluster_name}-eks-node-role-policy-attachment-{i}",
        policy_arn=policy,
        role=node_role.id,
        opts=ResourceOptions(parent=node_role),
    )
node_instance_profile = aws.iam.InstanceProfile(
    f"{cluster_name}-eks-node-instance-profile",
    role=node_role.name,
    path=f"/ol-infrastructure/eks/{cluster_name}/",
)

export("node_instance_profile", node_instance_profile.id)
export("node_role_arn", value=node_role.arn)

############################################################
# Create access entry for self-managed node groups
############################################################
# When authentication_mode="API", self-managed node groups require an explicit
# access entry with type="EC2_LINUX" to join the cluster.
# This uses conditional import to handle existing access entries gracefully.
#
# We need to predict the node role ARN since it hasn't been created yet.
# The pattern is: arn:aws:iam::{account_id}:role{path}{role_name}
# Since role uses name_prefix, we look for any role matching the path pattern.


def get_node_role_arn_pattern(cluster_name: str):
    """Get the ARN pattern for node role to check for existing access entry."""
    # Node roles are in the path /ol-infrastructure/eks/{cluster_name}/
    # We'll try to find existing role with this path and name pattern
    path = f"/ol-infrastructure/eks/{cluster_name}/"
    iam_client = boto3.client("iam")
    try:
        roles = iam_client.list_roles(PathPrefix=path)["Roles"]
        for role in roles:
            if role["RoleName"].startswith(
                f"{cluster_name}-eks-node-role-"[:IAM_ROLE_NAME_PREFIX_MAX_LENGTH]
            ):
                role_detail = iam_client.get_role(RoleName=role["RoleName"])
                if {"Key": "cluster", "Value": cluster_name} not in role_detail["Role"][
                    "Tags"
                ]:
                    return role["Arn"]
    except ClientError:
        # Role doesn't exist yet, will create new entry
        return None
    return None


node_role_arn_pattern = get_node_role_arn_pattern(cluster_name)

if node_role_arn_pattern:
    node_access_entry_opts, _ = access_entry_opts(
        cluster_name=cluster_name,
        principal_arn=node_role_arn_pattern,
    )
else:
    node_access_entry_opts = ResourceOptions()

node_access_entry = aws.eks.AccessEntry(
    f"{cluster_name}-eks-node-access-entry",
    cluster_name=cluster.eks_cluster.name,
    principal_arn=node_role.arn,
    type="EC2_LINUX",
    tags=aws_config.merged_tags({"cluster": cluster_name}),
    opts=node_access_entry_opts.merge(
        ResourceOptions(
            depends_on=[cluster, node_role],
        )
    ),
)

# Initalize the k8s pulumi provider
k8s_global_labels = {
    "pulumi_managed": "true",
    "pulumi_stack": stack_info.full_name,
}
k8s_provider = kubernetes.Provider(
    "k8s-provider",
    kubeconfig=cluster.kubeconfig,
    opts=ResourceOptions(parent=cluster, depends_on=[cluster, administrator_role]),
)

# Loop through the node group definitions and add them to the cluster
node_groups = []
for ng_name, ng_config in eks_config.require_object("nodegroups").items():
    taint_list = {}
    for taint_name, taint_config in ng_config["taints"].items() or {}:
        taint_list[taint_name] = eks.TaintArgs(
            value=taint_config["value"],
            effect=taint_config["effect"],
        )
    node_group_sec_group = eks.NodeGroupSecurityGroup(
        f"{cluster_name}-eks-nodegroup-{ng_name}-secgroup",
        cluster_security_group=cluster.cluster_security_group,
        eks_cluster=cluster.eks_cluster,
        vpc_id=target_vpc["id"],
        tags=aws_config.tags,
    )
    # Even though this is in the loop, it will only export the first one (the 'core nodes')
    export("node_group_security_group_id", node_group_sec_group.security_group.id)

    node_groups.append(
        eks.NodeGroupV2(
            f"{cluster_name}-eks-nodegroup-{ng_name}",
            cluster=eks.CoreDataArgs(
                cluster=cluster.eks_cluster,
                cluster_iam_role=cluster_role,
                endpoint=cluster.eks_cluster.endpoint,
                instance_roles=[node_role],
                node_group_options=eks.ClusterNodeGroupOptionsArgs(
                    node_associate_public_ip_address=False,
                ),
                provider=k8s_provider,
                subnet_ids=target_vpc["k8s_pod_subnet_ids"],
                vpc_id=target_vpc["id"],
            ),
            launch_template_tag_specifications=[
                aws.ec2.LaunchTemplateTagSpecificationArgs(
                    resource_type="instance",
                    tags=aws_config.tags,
                ),
                aws.ec2.LaunchTemplateTagSpecificationArgs(
                    resource_type="volume",
                    tags=aws_config.tags,
                ),
            ],
            gpu=ng_config.get("gpu") or False,
            min_refresh_percentage=eks_config.get_int("min_refresh_percentage") or 90,
            instance_type=ng_config["instance_type"],
            instance_profile=node_instance_profile,
            labels=ng_config["labels"] or {},
            node_security_group=node_group_sec_group.security_group,
            node_root_volume_size=ng_config["disk_size_gb"] or 250,
            node_root_volume_delete_on_termination=True,
            node_root_volume_type="gp3",
            cluster_ingress_rule=node_group_sec_group.security_group_rule,
            desired_capacity=ng_config["scaling"]["desired"] or 3,
            max_size=ng_config["scaling"]["max"] or 5,
            min_size=ng_config["scaling"]["min"] or 2,
            taints=taint_list,
            opts=ResourceOptions(parent=cluster, depends_on=cluster),
        )
    )

    allow_all_ingress_from_pod_cidrs = aws.ec2.SecurityGroupRule(
        f"{cluster_name}-eks-nodegroup-{ng_name}-all-ingress-from-pod-cidrs",
        type="ingress",
        description="Allow all traffic from pod CIDRs",
        security_group_id=node_group_sec_group.security_group.id,
        protocol="-1",
        from_port=0,
        to_port=0,
        cidr_blocks=pod_ip_blocks,
    )


# Every cluster gets an 'operations' namespace.
# It acts as a parent resource to many other resources below.
operations_namespace = kubernetes.core.v1.Namespace(
    resource_name=f"{cluster_name}-operations-namespace",
    metadata=kubernetes.meta.v1.ObjectMetaArgs(
        name="operations",
        labels=k8s_global_labels,
    ),
    opts=ResourceOptions(
        provider=k8s_provider,
        protect=False,
    ),
)

# Create any requested namespaces defined for the cluster
for namespace in namespaces:
    resource_name = (f"{cluster_name}-{namespace}-namespace",)
    kubernetes.core.v1.Namespace(
        resource_name=f"{cluster_name}-{namespace}-namespace",
        metadata=kubernetes.meta.v1.ObjectMetaArgs(
            name=namespace,
            labels=k8s_global_labels,
        ),
        opts=ResourceOptions(
            provider=k8s_provider, protect=False, depends_on=[*node_groups]
        ),
    )
export("namespaces", [*namespaces, "operations"])

############################################################
# Install custom resource definitions for prometheus operator configs
############################################################
# Install CRDs for Prometheus Operator (ServiceMonitors and PodMonitors)
# These are typically bundled with kube-prometheus-stack, but we install them separately
# to allow other tools or lighter-weight Prometheus setups to use them.
#
# We install just the four custom resource definitions that alloy supports
PROMETHEUS_OPERATOR_CRD_BASE_URL = f"https://raw.githubusercontent.com/prometheus-operator/prometheus-operator/{VERSIONS['PROMETHEUS_OPERATOR']}/example/prometheus-operator-crd"

prometheus_operator_crds = kubernetes.yaml.v2.ConfigGroup(
    f"{cluster_name}-prometheus-operator-crds",
    files=[
        f"{PROMETHEUS_OPERATOR_CRD_BASE_URL}/monitoring.coreos.com_podmonitors.yaml",
        f"{PROMETHEUS_OPERATOR_CRD_BASE_URL}/monitoring.coreos.com_servicemonitors.yaml",
        f"{PROMETHEUS_OPERATOR_CRD_BASE_URL}/monitoring.coreos.com_prometheusrules.yaml",
        f"{PROMETHEUS_OPERATOR_CRD_BASE_URL}/monitoring.coreos.com_probes.yaml",
    ],
    opts=ResourceOptions(
        provider=k8s_provider, delete_before_replace=True, depends_on=[*node_groups]
    ),
)


############################################################
# Configure CSI Drivers
############################################################
csi_driver_role_parliament_config = {
    "UNKNOWN_FEDERATION_SOURCE": {"ignore_locations": [{"principal": "federated"}]},
    "PERMISSIONS_MANAGEMENT_ACTIONS": {"ignore_locations": []},
    "MALFORMED": {"ignore_lcoations": []},
}

############################################################
# Setup EBS CSI provisioner
############################################################
# Ref: https://docs.aws.amazon.com/eks/latest/userguide/ebs-csi.html
export("has_ebs_storage", eks_config.get_bool("ebs_csi_provisioner"))
if eks_config.get_bool("ebs_csi_provisioner"):
    ebs_csi_driver_role_config = OLEKSTrustRoleConfig(
        account_id=aws_account.account_id,
        cluster_name=cluster_name,
        cluster_identities=cluster.eks_cluster.identities,
        description="Trust role for allowing the ebs csi driver to provision storage "
        "from within the cluster.",
        policy_operator="StringEquals",
        role_name="ebs-csi-driver",
        service_account_identifier="system:serviceaccount:kube-system:ebs-csi-controller-sa",
        tags=aws_config.tags,
    )
    ebs_csi_driver_role = OLEKSTrustRole(
        f"{cluster_name}-ebs-csi-driver-trust-role",
        role_config=ebs_csi_driver_role_config,
        opts=ResourceOptions(parent=cluster, depends_on=cluster),
    )
    ebs_csi_driver_kms_for_encryption_policy = aws.iam.Policy(
        f"{cluster_name}-ebs-csi-driver-kms-for-encryption-policy",
        name=f"{cluster_name}-ebs-csi-driver-kms-for-encryption-policy",
        path=f"/ol-infrastructure/eks/{cluster_name}/",
        policy=kms_ebs.apply(
            lambda kms_config: lint_iam_policy(
                {
                    "Version": IAM_POLICY_VERSION,
                    "Statement": [
                        {
                            "Effect": "Allow",
                            "Action": [
                                "kms:CreateGrant",
                                "kms:ListGrants",
                                "kms:RevokeGrant",
                            ],
                            "Resource": [kms_config["arn"]],
                            "Condition": {
                                "Bool": {"kms:GrantIsForAWSResource": "true"}
                            },
                        },
                        {
                            "Effect": "Allow",
                            "Action": [
                                "kms:Encrypt",
                                "kms:Decrypt",
                                "kms:ReEncrypt*",
                                "kms:GenerateDataKey*",
                                "kms:DescribeKey",
                            ],
                            "Resource": [kms_config["arn"]],
                        },
                    ],
                },
                parliament_config=csi_driver_role_parliament_config,
                stringify=True,
            )
        ),
        opts=ResourceOptions(parent=ebs_csi_driver_role, depends_on=cluster),
    )
    aws.iam.RolePolicyAttachment(
        f"{cluster_name}-ebs-csi-driver-kms-policy-attachment",
        policy_arn=ebs_csi_driver_kms_for_encryption_policy.arn,
        role=ebs_csi_driver_role.role.id,
        opts=ResourceOptions(parent=ebs_csi_driver_role),
    )
    aws.iam.RolePolicyAttachment(
        f"{cluster_name}-ebs-csi-driver-EBSCSIDriverPolicy-attachment",
        policy_arn="arn:aws:iam::aws:policy/service-role/AmazonEBSCSIDriverPolicy",
        role=ebs_csi_driver_role.role.id,
        opts=ResourceOptions(parent=ebs_csi_driver_role),
    )

    # Default storageclass configured for nominal performance
    kubernetes.storage.v1.StorageClass(
        resource_name=f"{cluster_name}-ebs-gp3-storageclass",
        metadata=kubernetes.meta.v1.ObjectMetaArgs(
            name="ebs-gp3-sc",
            labels=k8s_global_labels,
            annotations={"storageclass.kubernetes.io/is-default-class": "true"},
        ),
        provisioner="ebs.csi.aws.com",
        volume_binding_mode="WaitForFirstConsumer",
        # ref: https://github.com/kubernetes-sigs/aws-ebs-csi-driver/blob/master/docs/parameters.md
        parameters={
            "csi.storage.k8s.io/fstype": "xfs",
            "type": "gp3",
            "iopsPerGB": "50",
            "throughput": "125",
            "encrypted": "true",
        },
        opts=ResourceOptions(
            provider=k8s_provider,
            depends_on=[ebs_csi_driver_role, *node_groups],
        ),
    )
    aws_ebs_cni_driver_addon = eks.Addon(
        f"{cluster_name}-eks-addon-ebs-cni-driver-addon",
        cluster=cluster,
        addon_name="aws-ebs-csi-driver",
        addon_version=get_eks_addon_version("aws-ebs-csi-driver"),
        service_account_role_arn=ebs_csi_driver_role.role.arn,
        opts=ResourceOptions(
            parent=cluster,
            # Addons won't install properly if there are not nodes to schedule them on
            depends_on=[cluster, *node_groups],
        ),
    )
    export("ebs_storageclass", "ebs-gp3-sc")

############################################################
# Setup EFS CSI Provisioner
############################################################
# Ref: https://docs.aws.amazon.com/eks/latest/userguide/efs-csi.html
# Ref: https://github.com/kubernetes-sigs/aws-efs-csi-driver/blob/master/docs/efs-create-filesystem.md
export("has_efs_storage", eks_config.get_bool("efs_csi_provisioner"))
if eks_config.get_bool("efs_csi_provisioner"):
    efs_csi_driver_role_config = OLEKSTrustRoleConfig(
        account_id=aws_account.account_id,
        cluster_name=cluster_name,
        cluster_identities=cluster.eks_cluster.identities,
        description="Trust role for allowing the efs csi driver to provision storage "
        "from within the cluster.",
        policy_operator="StringLike",
        role_name="efs-csi-driver",
        service_account_identifier="system:serviceaccount:kube-system:efs-csi-*",
        tags=aws_config.tags,
    )
    efs_csi_driver_role = OLEKSTrustRole(
        f"{cluster_name}-efs-csi-driver-trust-role",
        role_config=efs_csi_driver_role_config,
        opts=ResourceOptions(parent=cluster, depends_on=cluster),
    )
    aws.iam.RolePolicyAttachment(
        f"{cluster_name}-efs-csi-driver-EFSCSIDriverPolicy-attachment",
        policy_arn="arn:aws:iam::aws:policy/service-role/AmazonEFSCSIDriverPolicy",
        role=efs_csi_driver_role.role.id,
        opts=ResourceOptions(parent=efs_csi_driver_role),
    )

    efs_filesystem = aws.efs.FileSystem(
        f"{cluster_name}-eks-filesystem",
        encrypted=True,
        kms_key_id=kms_ebs["arn"],
        tags=aws_config.tags,
        throughput_mode="bursting",
        opts=ResourceOptions(parent=cluster, depends_on=cluster),
    )

    efs_security_group = aws.ec2.SecurityGroup(
        f"{cluster_name}-eks-efs-securitygroup",
        description="Allows the EKS subnets to access EFS",
        vpc_id=target_vpc["id"],
        ingress=[
            aws.ec2.SecurityGroupIngressArgs(
                protocol="tcp",
                from_port=DEFAULT_EFS_PORT,
                to_port=DEFAULT_EFS_PORT,
                cidr_blocks=target_vpc["k8s_pod_subnet_cidrs"],
                description=f"Allow traffic from EKS nodes on port {DEFAULT_EFS_PORT}",
            ),
        ],
    )

    def __create_mountpoints(pod_subnet_ids):
        for index, subnet_id in enumerate(pod_subnet_ids):
            aws.efs.MountTarget(
                f"{cluster_name}-eks-mounttarget-{index}",
                file_system_id=efs_filesystem.id,
                subnet_id=subnet_id,
                security_groups=[efs_security_group.id],
                opts=ResourceOptions(parent=efs_filesystem),
            )

    pod_subnet_ids.apply(lambda pod_subnet_ids: __create_mountpoints(pod_subnet_ids))

    kubernetes.storage.v1.StorageClass(
        resource_name=f"{cluster_name}-efs-storageclass",
        metadata=kubernetes.meta.v1.ObjectMetaArgs(
            name="efs-sc",
            labels=k8s_global_labels,
        ),
        provisioner="efs.csi.aws.com",
        volume_binding_mode="WaitForFirstConsumer",
        # ref: https://github.com/kubernetes-sigs/aws-efs-csi-driver/blob/master/docs/README.md
        parameters={
            "provisioningMode": "efs-ap",
            "fileSystemId": efs_filesystem.id,
            "directoryPerms": "700",
        },
        opts=ResourceOptions(
            provider=k8s_provider,
            depends_on=[efs_csi_driver_role, *node_groups],
        ),
    )
    aws_efs_cni_driver_addon = eks.Addon(
        f"{cluster_name}-eks-addon-efs-cni-driver-addon",
        cluster=cluster,
        addon_name="aws-efs-csi-driver",
        addon_version=get_eks_addon_version("aws-efs-csi-driver"),
        service_account_role_arn=efs_csi_driver_role.role.arn,
        opts=ResourceOptions(
            parent=cluster,
            # Addons won't install properly if there are not nodes to schedule them on
            depends_on=[cluster, *node_groups],
        ),
    )
    export("efs_storageclass", "efs-sc")


setup_vault_secrets_operator(
    cluster_name=cluster_name,
    cluster=cluster,
    k8s_provider=k8s_provider,
    operations_namespace=operations_namespace,
    node_groups=node_groups,
    stack_info=stack_info,
    k8s_global_labels=k8s_global_labels,
    operations_tolerations=operations_tolerations,
    versions=VERSIONS,
)

setup_external_dns(
    cluster_name=cluster_name,
    cluster=cluster,
    aws_account=aws_account,
    aws_config=aws_config,
    k8s_provider=k8s_provider,
    operations_namespace=operations_namespace,
    node_groups=node_groups,
    k8s_global_labels=k8s_global_labels,
    operations_tolerations=operations_tolerations,
    versions=VERSIONS,
    eks_config=eks_config,
)

cert_manager_release = setup_cert_manager(
    cluster_name=cluster_name,
    cluster=cluster,
    aws_account=aws_account,
    aws_config=aws_config,
    k8s_provider=k8s_provider,
    operations_namespace=operations_namespace,
    node_groups=node_groups,
    k8s_global_labels=k8s_global_labels,
    operations_tolerations=operations_tolerations,
    versions=VERSIONS,
)

create_core_dns_resources(
    cluster_name=cluster_name,
    k8s_global_labels=k8s_global_labels,
    k8s_provider=k8s_provider,
    cluster=cluster,
    node_groups=node_groups,
)

############################################################
# Setup AWS integrations
# AWS Load Balancer Controller, AWS Node Termination Handler
############################################################
lb_controller = setup_aws_integrations(
    aws_account=aws_account,
    cluster_name=cluster_name,
    cluster=cluster,
    aws_config=aws_config,
    k8s_global_labels=k8s_global_labels,
    k8s_provider=k8s_provider,
    operations_tolerations=operations_tolerations,
    target_vpc=target_vpc,
    node_groups=node_groups,
    versions=VERSIONS,
    cert_manager=cert_manager_release,
)

gateway_api_crds = setup_traefik(
    cluster_name=cluster_name,
    k8s_provider=k8s_provider,
    operations_namespace=operations_namespace,
    node_groups=node_groups,
    prometheus_operator_crds=prometheus_operator_crds,
    k8s_global_labels=k8s_global_labels,
    operations_tolerations=operations_tolerations,
    versions=VERSIONS,
    eks_config=eks_config,
    target_vpc=target_vpc,
    aws_config=aws_config,
    cluster=cluster,
    lb_controller=lb_controller,
)

setup_apisix(
    cluster_name=cluster_name,
    k8s_provider=k8s_provider,
    operations_namespace=operations_namespace,
    node_groups=node_groups,
    gateway_api_crds=gateway_api_crds,
    stack_info=stack_info,
    k8s_global_labels=k8s_global_labels,
    operations_tolerations=operations_tolerations,
    versions=VERSIONS,
    eks_config=eks_config,
    target_vpc=target_vpc,
    aws_config=aws_config,
    cluster=cluster,
    lb_controller=lb_controller,
)


############################################################
# Install and configure metrics-server
############################################################
metrics_server_release = kubernetes.helm.v3.Release(
    f"{cluster_name}-metrics-server-helm-release",
    kubernetes.helm.v3.ReleaseArgs(
        name="metrics-server",
        chart="metrics-server",
        namespace="kube-system",
        repository_opts=kubernetes.helm.v3.RepositoryOptsArgs(
            repo="https://kubernetes-sigs.github.io/metrics-server/",
        ),
        cleanup_on_fail=True,
        skip_await=False,
        values={
            "commonLabels": k8s_global_labels,
            "tolerations": operations_tolerations,
            "resources": {
                "requests": {
                    "memory": "100Mi",
                    "cpu": "25m",
                },
                "limits": {
                    "memory": "100Mi",
                },
            },
        },
    ),
    opts=ResourceOptions(
        provider=k8s_provider,
        parent=cluster,
        depends_on=[node_groups[0]],
        delete_before_replace=True,
    ),
)
