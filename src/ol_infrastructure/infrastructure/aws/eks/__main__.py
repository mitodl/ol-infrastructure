# ruff: noqa: ERA001

import json

import pulumi_aws as aws
import pulumi_eks as eks
import pulumi_kubernetes as kubernetes
import pulumi_vault as vault
from pulumi import Config, ResourceOptions, StackReference, export

from bridge.lib.magic_numbers import IAM_ROLE_NAME_PREFIX_MAX_LENGTH
from ol_infrastructure.lib.aws.iam_helper import (
    IAM_POLICY_VERSION,
    eks_ebs_oidc_trust_policy_template,
    eks_efs_oidc_trust_policy_template,
    lint_iam_policy,
)
from ol_infrastructure.lib.ol_types import AWSBase
from ol_infrastructure.lib.pulumi_helper import parse_stack
from ol_infrastructure.lib.vault import setup_vault_provider

eks_config = Config("eks")
env_config = Config("environment")
vault_config = Config("vault")

stack_info = parse_stack()
setup_vault_provider(stack_info)
aws_account = aws.get_caller_identity()

network_stack = StackReference(f"infrastructure.aws.network.{stack_info.name}")
vault_stack = StackReference(f"infrastructure.vault.operations.{stack_info.name}")
kms_stack = StackReference(f"infrastructure.aws.kms.{stack_info.name}")
policy_stack = StackReference("infrastructure.aws.policies")

business_unit = env_config.require("business_unit") or "operations"
target_vpc = network_stack.require_output(env_config.require("target_vpc"))

pod_ip_blocks = target_vpc["k8s_pod_subnet_cidrs"]
pod_subnet_ids = target_vpc["k8s_pod_subnet_ids"]
service_ip_block = target_vpc["k8s_service_subnet_cidr"]

cluster_name = f"{stack_info.env_prefix}-{stack_info.env_suffix}"

kms_ebs = kms_stack.require_output("kms_ec2_ebs_key")

default_addons = {
    #    "aws-node" - daemonset
    #    "coredns" - deployment
    #    "kube-proxy" - daemonset
}

aws_config = AWSBase(
    tags={
        "OU": env_config.get("business_unit") or "operations",
        "Environment": cluster_name,
        "Owner": "platform-engineering",
    },
)
AWS_REGION = aws_config.region

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
    policy_stack.require_output("iam_policies")["describe_instances"],
]
if eks_config.get_bool("ebs_csi_provisioner"):
    managed_policy_arns.append(
        "arn:aws:iam::aws:policy/service-role/AmazonEBSCSIDriverPolicy"
    )
if eks_config.get_bool("efs_csi_provisioner"):
    managed_policy_arns.append(
        "arn:aws:iam::aws:policy/service-role/AmazonEFSCSIDriverPolicy"
    )
for i, policy in enumerate(managed_policy_arns):
    aws.iam.RolePolicyAttachment(
        f"{cluster_name}-eks-node-role-policy-attachment-{i}",
        policy_arn=policy,
        role=node_role.id,
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
        enable_imd_sv2=True,
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


# Initalize the k8s pulumi provider and configure the central operations namespace
k8s_global_labels = {
    "pulumi_managed": "true",
}
k8s_provider = kubernetes.Provider(
    "k8s-provider",
    kubeconfig=cluster.kubeconfig,
    opts=ResourceOptions(depends_on=[cluster]),
)

operations_namespace = kubernetes.core.v1.Namespace(
    resource_name=f"{cluster_name}-operations-namespace",
    metadata=kubernetes.meta.v1.ObjectMetaArgs(
        name="operations",
        labels=k8s_global_labels,
    ),
    opts=ResourceOptions(provider=k8s_provider, parent=k8s_provider),
)

# Create any requested additional namespaces
for namespace in eks_config.get_object("namespaces") or []:
    resource_name = (f"{cluster_name}-{namespace}-namespace",)
    kubernetes.core.v1.Namespace(
        resource_name=f"{cluster_name}-{namespace}-namespace",
        metadata=kubernetes.meta.v1.ObjectMetaArgs(
            name=namespace,
            labels=k8s_global_labels,
        ),
        opts=ResourceOptions(provider=k8s_provider, parent=k8s_provider),
    )

# Configure CSI Drivers
csi_driver_role_parliament_config = {
    "UNKNOWN_FEDERATION_SOURCE": {"ignore_locations": [{"principal": "federated"}]},
    "PERMISSIONS_MANAGEMENT_ACTIONS": {"ignore_locations": []},
    "MALFORMED": {"ignore_lcoations": []},
}

# Setup EBS CSI provisioner
# Ref: https://docs.aws.amazon.com/eks/latest/userguide/ebs-csi.html
if eks_config.get_bool("ebs_csi_provisioner"):
    ebs_csi_driver_role = aws.iam.Role(
        f"{cluster_name}-ebs-csi-driver-trust-role",
        name=f"{cluster_name}-ebs-csi-driver-trust-role",
        path=f"/ol-infrastructure/eks/{cluster_name}/",
        assume_role_policy=cluster.eks_cluster.identities.apply(
            lambda ids: lint_iam_policy(
                eks_ebs_oidc_trust_policy_template(
                    oidc_identifier=ids[0]["oidcs"][0]["issuer"],
                    account_id=aws_account.account_id,
                    k8s_service_account_identifier="system:serviceaccount:kube-system:ebs-csi-controller-sa",
                ),
                parliament_config=csi_driver_role_parliament_config,
                stringify=True,
            )
        ),
        description="Trust role for allowing the EBS CSI driver to provision storage "
        "within the cluster.",
        opts=ResourceOptions(parent=cluster),
    )

    ebs_csi_driver_kms_for_encryption_policy = aws.iam.Policy(
        f"{cluster_name}-ebs-csi-driver-kms-for-encryption-policy",
        name=f"{cluster_name}-ebs-csi-driver-kms-for-encryption-policy",
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
    )
    aws.iam.RolePolicyAttachment(
        f"{cluster_name}-ebs-csi-driver-kms-policy-attachment",
        policy_arn=ebs_csi_driver_kms_for_encryption_policy.arn,
        role=ebs_csi_driver_role.id,
    )
    aws.iam.RolePolicyAttachment(
        f"{cluster_name}-ebs-csi-driver-EBSCSIDriverPolicy-attachment",
        policy_arn="arn:aws:iam::aws:policy/service-role/AmazonEBSCSIDriverPolicy",
        role=ebs_csi_driver_role.id,
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
        opts=ResourceOptions(provider=k8s_provider, parent=k8s_provider),
    )

    default_addons["aws-ebs-csi-driver"] = {
        "addon_name": "aws-ebs-csi-driver",
        "addon_version": "v1.33.0-eksbuild.1",
        "service_account_role_arn": ebs_csi_driver_role.arn,
    }

# Setup EFS CSI Provisioner
if eks_config.get_bool("efs_csi_provisioner"):
    efs_csi_driver_role = aws.iam.Role(
        f"{cluster_name}-efs-csi-driver-trust-role",
        name=f"{cluster_name}-efs-csi-driver-trust-role",
        path=f"/ol-infrastructure/eks/{cluster_name}/",
        assume_role_policy=cluster.eks_cluster.identities.apply(
            lambda ids: lint_iam_policy(
                eks_efs_oidc_trust_policy_template(
                    oidc_identifier=ids[0]["oidcs"][0]["issuer"],
                    account_id=aws_account.account_id,
                    k8s_service_account_identifier="system:serviceaccount:kube-system:efs-csi-*",
                ),
                parliament_config=csi_driver_role_parliament_config,
                stringify=True,
            )
        ),
        description="Trust role for allowing the EFS CSI driver to provision storage "
        "within the cluster.",
        opts=ResourceOptions(parent=cluster),
    )
    aws.iam.RolePolicyAttachment(
        f"{cluster_name}-efs-csi-driver-EFSCSIDriverPolicy-attachment",
        policy_arn="arn:aws:iam::aws:policy/service-role/AmazonEFSCSIDriverPolicy",
        role=efs_csi_driver_role.id,
    )

    efs_filesystem = aws.efs.FileSystem(
        f"{cluster_name}-eks-filesystem",
        encrypted=True,
        kms_key_id=kms_ebs["arn"],
        tags=aws_config.tags,
        throughput_mode="bursting",
    )

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
        opts=ResourceOptions(provider=k8s_provider, parent=k8s_provider),
    )
    default_addons["aws-efs-csi-driver"] = {
        "addon_name": "aws-efs-csi-driver",
        "addon_version": "v2.0.7-eksbuild.1",
        "service_account_role_arn": efs_csi_driver_role.arn,
    }

# Configure addons
# Commented out means we let the cluster manages it automatically
extra_addons = eks_config.get_object("extra_addons") or {}
addons = default_addons | extra_addons
for addon_key, addon_definition in addons.items():
    eks.Addon(
        f"{cluster_name}-eks-addon-{addon_key}",
        cluster=cluster,
        **addon_definition,
    )

# Configure vault auth backend and add the vault secrets operator if requested
if eks_config.get_bool("vault_secrets_operator") or False:
    vault_k8s_auth = vault.AuthBackend(
        "vault-k8s-auth-backend",
        type="kubernetes",
        path=f"k8s-{cluster_name}",
    )
    vault_k8s_auth_backend_config = vault.kubernetes.AuthBackendConfig(
        f"{cluster_name}-eks-vault-authentication-configuration-operations",
        kubernetes_host=cluster.eks_cluster.endpoint,
        backend=f"k8s-{cluster_name}",
        disable_local_ca_jwt=True,
        opts=ResourceOptions(parent=vault_k8s_auth),
    )

    vault_k8s_auth_backend_role = vault.kubernetes.AuthBackendRole(
        f"{cluster_name}-eks-vault-authentication-endpoint-operations",
        role_name=f"{cluster_name}-operations",
        backend=f"k8s-{cluster_name}",
        bound_service_account_names=["*"],
        bound_service_account_namespaces=["kube-system", "operations"],
        opts=ResourceOptions(parent=vault_k8s_auth),
    )

    vault_secrets_operator = kubernetes.helm.v3.Release(
        f"{cluster_name}-vault-secrets-operator-release",
        kubernetes.helm.v3.ReleaseArgs(
            name="vault-secrets-operator",
            chart="vault-secrets-operator",
            version="0.8.1",
            namespace="operations",
            repository_opts=kubernetes.helm.v3.RepositoryOptsArgs(
                repo="https://helm.releases.hashicorp.com",
            ),
            values={
                "defaultVaultConnection": {
                    "enabled": True,
                    "address": vault_config.get("address"),
                    "skipTLSVerify": False,
                },
                "defaultAuthMethod": {
                    "enabled": True,
                    "mount": f"k8s-{cluster_name}",
                    "kubernetes": {
                        "role": f"{cluster_name}-operations",
                    },
                    "allowed_namespaces": ["kube-system", "operations"],
                },
            },
            skip_await=False,
        ),
        opts=ResourceOptions(
            provider=k8s_provider,
            parent=operations_namespace,
            delete_before_replace=True,
        ),
    )
    export("vault-secrets-operator-name", vault_secrets_operator.name)
    export("oidc_identifier", cluster.eks_cluster.identities)
