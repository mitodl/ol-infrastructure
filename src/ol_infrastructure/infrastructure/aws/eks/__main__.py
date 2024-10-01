# ruff: noqa: ERA001, TD003, TD002, TD004 FIX002, E501

# Misc Ref: https://docs.aws.amazon.com/eks/latest/userguide/associate-service-account-role.html

import base64
import json

import pulumi_aws as aws
import pulumi_eks as eks
import pulumi_kubernetes as kubernetes
import pulumi_vault as vault
from pulumi import Config, ResourceOptions, StackReference, export

from bridge.lib.magic_numbers import DEFAULT_EFS_PORT, IAM_ROLE_NAME_PREFIX_MAX_LENGTH
from ol_infrastructure.lib.aws.iam_helper import (
    EKS_ADMIN_USERNAMES,
    IAM_POLICY_VERSION,
    lint_iam_policy,
    oidc_trust_policy_template,
)
from ol_infrastructure.lib.ol_types import AWSBase
from ol_infrastructure.lib.pulumi_helper import parse_stack
from ol_infrastructure.lib.vault import setup_vault_provider

############################################################
# Configuration defining / loading and prep work
############################################################


eks_config = Config("eks")
env_config = Config("environment")
vault_config = Config("vault")

stack_info = parse_stack()
setup_vault_provider(stack_info)
aws_account = aws.get_caller_identity()

dns_stack = StackReference("infrastructure.aws.dns")
iam_stack = StackReference("infrastructure.aws.iam")
kms_stack = StackReference(f"infrastructure.aws.kms.{stack_info.name}")
network_stack = StackReference(f"infrastructure.aws.network.{stack_info.name}")
policy_stack = StackReference("infrastructure.aws.policies")
vault_stack = StackReference(f"infrastructure.vault.operations.{stack_info.name}")

business_unit = env_config.require("business_unit") or "operations"
target_vpc = network_stack.require_output(env_config.require("target_vpc"))

pod_ip_blocks = target_vpc["k8s_pod_subnet_cidrs"]
pod_subnet_ids = target_vpc["k8s_pod_subnet_ids"]
service_ip_block = target_vpc["k8s_service_subnet_cidr"]

cluster_name = f"{stack_info.env_prefix}-{stack_info.env_suffix}"

kms_ebs = kms_stack.require_output("kms_ec2_ebs_key")

# Centralize version numbers
VERSIONS = {
    "CERT_MANAGER_CHART": "v1.16.0-beta.0",
    "EBS_CSI_DRIVER": "v1.33.0-eksbuild.1",
    "EFS_CSI_DRIVER": "v2.0.7-eksbuild.1",
    "GATEWAY_API": "v1.1.0",
    "EXTERNAL_DNS_CHART": "1.15.0",
    "TRAEFIK_CHART": "v31.0.0",
    "VAULT_SECRETS_OPERATOR_CHART": "0.8.1",
    "VAULT_SECRETS_OPERATOR": "0.8.1",
    "OPEN_METADATA": "1.5.5",
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

admin_assume_role_policy = {
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
                ],
            },
        }
    ],
}

############################################################
# create core IAM resources
############################################################
# IAM role that admins will assume when using kubectl
administrator_role = aws.iam.Role(
    f"{cluster_name}-eks-admin-role",
    assume_role_policy=json.dumps(admin_assume_role_policy),
    name_prefix=f"{cluster_name}-eks-admin-role-"[:IAM_ROLE_NAME_PREFIX_MAX_LENGTH],
    path=f"/ol-infrastructure/eks/{cluster_name}/",
    tags=aws_config.tags,
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
cluster = eks.Cluster(
    f"{cluster_name}-eks-cluster",
    name=cluster_name,
    service_role=cluster_role,
    access_entries={
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
    use_default_vpc_cni=False,
    # Ref: https://docs.aws.amazon.com/eks/latest/userguide/security-groups-pods-deployment.html
    # Ref: https://docs.aws.amazon.com/eks/latest/userguide/sg-pods-example-deployment.html
    vpc_cni_options=eks.cluster.VpcCniOptionsArgs(
        enable_pod_eni=eks_config.get_bool("pod_security_groups"),
        enable_prefix_delegation=eks_config.get_bool("pod_security_groups"),
        disable_tcp_early_demux=eks_config.get_bool("pod_security_groups"),
        log_level="INFO",
    ),
    enabled_cluster_log_types=[
        "api",
        "audit",
        "authenticator",
    ],
    opts=ResourceOptions(
        parent=cluster_role, depends_on=[cluster_role, administrator_role]
    ),
)
export("cluster_name", cluster_name)
export("admin_role_arn", administrator_role.arn)
export("cluster_ca", cluster.eks_cluster.certificate_authority)
export("cluster_endpoint", cluster.eks_cluster.endpoint)
export("kube_config", cluster.kubeconfig)

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
export("node_role_arn", value=node_role.arn)

# Loop through the node group definitions and add them to the cluster
node_groups = []
for ng_name, ng_config in eks_config.require_object("nodegroups").items():
    taint_list = []
    for taint_name, taint_config in ng_config["taints"].items() or {}:
        taint_list.append(
            aws.eks.NodeGroupTaintArgs(
                key=taint_name,
                value=taint_config["value"] or None,
                effect=taint_config["effect"],
            ),
        )
    node_groups.append(
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
            opts=ResourceOptions(parent=cluster, depends_on=cluster),
        )
    )


# Initalize the k8s pulumi provider and configure the central operations namespace
k8s_global_labels = {
    "pulumi_managed": "true",
    "pulumi_stack": stack_info.full_name,
}
k8s_provider = kubernetes.Provider(
    "k8s-provider",
    kubeconfig=cluster.kubeconfig,
    opts=ResourceOptions(parent=cluster, depends_on=[cluster, node_groups[0]]),
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
        provider=k8s_provider, parent=k8s_provider, depends_on=k8s_provider
    ),
)

# Create any requested namespaces defined for the cluster
namespaces = eks_config.get_object("namespaces") or []
for namespace in namespaces:
    resource_name = (f"{cluster_name}-{namespace}-namespace",)
    kubernetes.core.v1.Namespace(
        resource_name=f"{cluster_name}-{namespace}-namespace",
        metadata=kubernetes.meta.v1.ObjectMetaArgs(
            name=namespace,
            labels=k8s_global_labels,
        ),
        opts=ResourceOptions(
            provider=k8s_provider, parent=k8s_provider, depends_on=k8s_provider
        ),
    )
export("namespaces", [*namespaces, "operations"])

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
    ebs_csi_driver_role = aws.iam.Role(
        f"{cluster_name}-ebs-csi-driver-trust-role",
        name=f"{cluster_name}-ebs-csi-driver-trust-role",
        path=f"/ol-infrastructure/eks/{cluster_name}/",
        assume_role_policy=cluster.eks_cluster.identities.apply(
            lambda ids: lint_iam_policy(
                oidc_trust_policy_template(
                    oidc_identifier=ids[0]["oidcs"][0]["issuer"],
                    account_id=aws_account.account_id,
                    k8s_service_account_identifier="system:serviceaccount:kube-system:ebs-csi-controller-sa",
                    operator="StringEquals",
                ),
                parliament_config=csi_driver_role_parliament_config,
                stringify=True,
            )
        ),
        description="Trust role for allowing the EBS CSI driver to provision storage "
        "from within the cluster.",
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
        opts=ResourceOptions(parent=cluster, depends_on=cluster),
    )
    aws.iam.RolePolicyAttachment(
        f"{cluster_name}-ebs-csi-driver-kms-policy-attachment",
        policy_arn=ebs_csi_driver_kms_for_encryption_policy.arn,
        role=ebs_csi_driver_role.id,
        opts=ResourceOptions(parent=ebs_csi_driver_role),
    )
    aws.iam.RolePolicyAttachment(
        f"{cluster_name}-ebs-csi-driver-EBSCSIDriverPolicy-attachment",
        policy_arn="arn:aws:iam::aws:policy/service-role/AmazonEBSCSIDriverPolicy",
        role=ebs_csi_driver_role.id,
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
            parent=k8s_provider,
            depends_on=[k8s_provider, ebs_csi_driver_role],
        ),
    )
    aws_ebs_cni_driver_addon = eks.Addon(
        f"{cluster_name}-eks-addon-ebs-cni-driver-addon",
        cluster=cluster,
        addon_name="aws-ebs-csi-driver",
        addon_version=VERSIONS["EBS_CSI_DRIVER"],
        service_account_role_arn=ebs_csi_driver_role.arn,
        opts=ResourceOptions(
            parent=cluster,
            # Addons won't install properly if there are not nodes to schedule them on
            depends_on=[cluster, node_groups[0]],
        ),
    )

############################################################
# Setup EFS CSI Provisioner
############################################################
# Ref: https://docs.aws.amazon.com/eks/latest/userguide/efs-csi.html
# Ref: https://github.com/kubernetes-sigs/aws-efs-csi-driver/blob/master/docs/efs-create-filesystem.md
export("has_efs_storage", eks_config.get_bool("efs_csi_provisioner"))
if eks_config.get_bool("efs_csi_provisioner"):
    efs_csi_driver_role = aws.iam.Role(
        f"{cluster_name}-efs-csi-driver-trust-role",
        name=f"{cluster_name}-efs-csi-driver-trust-role",
        path=f"/ol-infrastructure/eks/{cluster_name}/",
        assume_role_policy=cluster.eks_cluster.identities.apply(
            lambda ids: lint_iam_policy(
                oidc_trust_policy_template(
                    oidc_identifier=ids[0]["oidcs"][0]["issuer"],
                    account_id=aws_account.account_id,
                    k8s_service_account_identifier="system:serviceaccount:kube-system:efs-csi-*",
                    operator="StringLike",
                ),
                parliament_config=csi_driver_role_parliament_config,
                stringify=True,
            )
        ),
        description="Trust role for allowing the EFS CSI driver to provision storage "
        "from within the cluster.",
        opts=ResourceOptions(parent=cluster, depends_on=cluster),
    )
    aws.iam.RolePolicyAttachment(
        f"{cluster_name}-efs-csi-driver-EFSCSIDriverPolicy-attachment",
        policy_arn="arn:aws:iam::aws:policy/service-role/AmazonEFSCSIDriverPolicy",
        role=efs_csi_driver_role.id,
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

    def create_mountpoints(pod_subnet_ids):
        for index, subnet_id in enumerate(pod_subnet_ids):
            aws.efs.MountTarget(
                f"{cluster_name}-eks-mounttarget-{index}",
                file_system_id=efs_filesystem.id,
                subnet_id=subnet_id,
                security_groups=[efs_security_group.id],
                opts=ResourceOptions(parent=efs_filesystem),
            )

    pod_subnet_ids.apply(lambda pod_subnet_ids: create_mountpoints(pod_subnet_ids))

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
            parent=k8s_provider,
            depends_on=[k8s_provider, efs_csi_driver_role],
        ),
    )
    aws_efs_cni_driver_addon = eks.Addon(
        f"{cluster_name}-eks-addon-efs-cni-driver-addon",
        cluster=cluster,
        addon_name="aws-efs-csi-driver",
        addon_version=VERSIONS["EFS_CSI_DRIVER"],
        service_account_role_arn=efs_csi_driver_role.arn,
        opts=ResourceOptions(
            parent=cluster,
            # Addons won't install properly if there are not nodes to schedule them on
            depends_on=[cluster, node_groups[0]],
        ),
    )

############################################################
# Configure vault-secrets-operator
############################################################
# Setup vault auth endpoint for the cluster.  Apps will need
# their own auth backend roles added to auth backend which
# we will export the name of below.
vault_auth_endpoint_name = f"k8s-{stack_info.env_prefix}"
vault_k8s_auth = vault.AuthBackend(
    f"{cluster_name}-eks-vault-k8s-auth-backend",
    type="kubernetes",
    path=vault_auth_endpoint_name,
    opts=ResourceOptions(
        parent=cluster, depends_on=cluster, delete_before_replace=True
    ),
)
vault_k8s_auth_backend_config = vault.kubernetes.AuthBackendConfig(
    f"{cluster_name}-eks-vault-authentication-configuration-operations",
    kubernetes_ca_cert=cluster.eks_cluster.certificate_authority.data.apply(
        lambda b64_cert: "{}".format(base64.b64decode(b64_cert).decode("utf-8"))
    ),  # Important
    kubernetes_host=cluster.eks_cluster.endpoint,
    backend=vault_auth_endpoint_name,
    disable_iss_validation=True,  # Important
    disable_local_ca_jwt=False,  # Important
    opts=ResourceOptions(parent=vault_k8s_auth),
)
export("vault_auth_endpoint", vault_auth_endpoint_name)

# Install the vault-secrets-operator directly from the public chart
vault_secrets_operator = kubernetes.helm.v3.Release(
    f"{cluster_name}-vault-secrets-operator-helm-release",
    kubernetes.helm.v3.ReleaseArgs(
        name="vault-secrets-operator",
        chart="vault-secrets-operator",
        version=VERSIONS["VAULT_SECRETS_OPERATOR_CHART"],
        namespace="operations",
        cleanup_on_fail=True,
        repository_opts=kubernetes.helm.v3.RepositoryOptsArgs(
            repo="https://helm.releases.hashicorp.com",
        ),
        values={
            "image": {
                "pullPolicy": "Always",
            },
            "extraLabels": k8s_global_labels,
            "defaultVaultConnection": {
                "enabled": True,
                "address": vault_config.get("address"),
                "skipTLSVerify": False,
            },
            "controller": {
                "replicas": 1,
                "tolerations": operations_tolerations,
            },
        },
        skip_await=False,
    ),
    opts=ResourceOptions(
        provider=k8s_provider,
        parent=operations_namespace,
        depends_on=[cluster, node_groups[0]],
        delete_before_replace=True,
    ),
)

############################################################
# This entire block of commented out code relates to
# security groups for pods and should be retained for the time being
############################################################
#    traefik_pod_security_group = aws.ec2.SecurityGroup(
#        f"{cluster_name}-eks-traefik-pod-securitygroup",
#        description="Allows bi-directional trafic between the traefik gateway controller and pods",
#        vpc_id=target_vpc["id"],
#        egress=default_egress_args,
#        ingress=[
#            aws.ec2.SecurityGroupIngressArgs(
#                protocol=-1,
#                from_port=0,
#                to_port=0,
#                self=True,
#                description=f"Allow traffic on all TCP ports from ourselves",
#            ),
#            aws.ec2.SecurityGroupIngressArgs(
#                protocol="tcp",
#                from_port=0,
#                to_port=65535,
#                self=True,
#                description=f"Allow traffic on all TCP ports from ourselves",
#            ),
#            aws.ec2.SecurityGroupIngressArgs(
#                protocol="udp",
#                from_port=0,
#                to_port=65535,
#                self=True,
#                description=f"Allow traffic on all UDP ports from ourselves",
#            ),
#        ],
#        opts=ResourceOptions(parent=cluster),
#    )
#
#    traefik_pod_security_group_policy = kubernetes.yaml.v2.ConfigGroup(
#        f"{cluster_name}-eks-traefik-pod-security-group-policy",
#        objs=[
#            {
#                "apiVersion": "vpcresources.k8s.aws/v1beta1",
#                "kind": "SecurityGroupPolicy",
#                "metadata": {
#                    "name": "traefik-gateway-controller-sgp",
#                    "namespace": "operations",
#                },
#                "spec": {
#                    "podSelector": {
#                        "matchLabels": {
#                            "traffic-gateway-controller-security-group": "True",
#                        },
#                    },
#                    "securityGroups": {
#                        "groupIds": [
#                            traefik_pod_security_group.id,
#                        ],
#                    },
#                },
#            },
#        ],
#        opts=ResourceOptions(
#            provider=k8s_provider,
#            parent=operations_namespace,
#            delete_before_replace=True,
#            depends_on=[cluster, node_groups[0], operations_namespace, traefik_pod_security_group],
#        ),
#    )
############################################################
# End security groups for pods stuff
############################################################

############################################################
# Install and configure the traefik gateway api controller
############################################################

# The custom resource definitions that come with the traefik helm chart
# don't install the experimental CRDs even if you say you want to use
# the experimental features. So we need to install them by hand
# and explicitly tell the traefik helm release below NOT
# to install any CRDS or we will get errors.
#
# TODO @Ardiea it would be nice if we could add the k8s_global_labels to these
gateway_api_crds = kubernetes.yaml.v2.ConfigGroup(
    f"{cluster_name}-gateway-api-experimental-crds",
    files=[
        f"https://github.com/kubernetes-sigs/gateway-api/releases/download/{VERSIONS['GATEWAY_API']}/experimental-install.yaml"
    ],
    opts=ResourceOptions(
        provider=k8s_provider,
        parent=operations_namespace,
        delete_before_replace=True,
        depends_on=[cluster],
    ),
)

# This helm release installs the traefik k8s gateway api controller
# which will server as the ingress point for ALL connections going into
# the applications installed on the cluster. No other publically listening
# services or load balancers should be configured on the cluster.
#
# This does NOT configure a default gateway or any httproutes within
# the cluster.
#
# Ref: https://gateway-api.sigs.k8s.io/reference/spec/
# Ref: https://doc.traefik.io/traefik/routing/providers/kubernetes-gateway/
# Ref: https://doc.traefik.io/traefik/providers/kubernetes-gateway/
#
# TODO: @Ardiea add the ability to define more ports in config.
traefik_helm_release = kubernetes.helm.v3.Release(
    f"{cluster_name}-traefik-gateway-controller-helm-release",
    kubernetes.helm.v3.ReleaseArgs(
        name="traefik-gateway-controller",
        chart="traefik",
        version=VERSIONS["TRAEFIK_CHART"],
        namespace="operations",
        skip_crds=False,
        cleanup_on_fail=True,
        repository_opts=kubernetes.helm.v3.RepositoryOptsArgs(
            repo="https://helm.traefik.io/traefik",
        ),
        values={
            "image": {
                "pullPolicy": "Always",
            },
            "commonLabels": k8s_global_labels,
            "tolerations": operations_tolerations,
            # Debug the traefik by turning off "DaemonSet"
            # and setting "replcias": 1
            "deployment": {
                "kind": "DaemonSet",
                "podLabels": {
                    # "traffic-gateway-controller-security-group": "True",
                },
                "additionalVolumes": [
                    {"name": "plugins"},
                ],
            },
            "additionalVolumeMounts": [
                {"name": "plugins", "mountPath": "/plugins-storage"},
            ],
            # Not supporting legacy ingress resources
            "kubernetesIngress": {
                "enabled": False,
            },
            # Do not create a default gateway
            "gateway": {
                "enabled": False,
            },
            "gatewayClass": {
                "enabled": True,
            },
            "providers": {
                "kubernetesGateway": {
                    "enabled": True,
                },
            },
            # These are important for external-dns to actually work
            "additionalArguments": [
                "--providers.kubernetesgateway.statusAddress.service.namespace=operations",
                "--providers.kubernetesgateway.statusAddress.service.name=traefik-gateway-controller",
                "--serverstransport.insecureskipverify",
            ],
            # Redirect all :80 to :443
            "ports": {
                "web": {
                    "port": 8000,
                    "expose": {
                        "default": True,
                    },
                    "exposedPort": 80,
                    "redirectTo": {
                        "port": "websecure",
                    },
                },
                "websecure": {
                    "port": 8443,
                    "expose": {
                        "default": True,
                    },
                    "exposedPort": 443,
                },
            },
            "logs": {
                "general": {
                    "level": "INFO",
                },
            },
            "resources": {
                "requests": {
                    "cpu": "100m",
                    "memory": "50Mi",
                },
                "limits": {
                    "cpu": "300m",
                    "memory": "150Mi",
                },
            },
            "service": {
                # These control the configuration of the network load balancer that EKS will create
                # automatically and point at every traefik pod.
                # Ref: https://kubernetes-sigs.github.io/aws-load-balancer-controller/v2.4/guide/service/annotations/#subnets
                "annotations": {
                    "service.beta.kubernetes.io/aws-load-balancer-type": "nlb",
                    "service.beta.kubernetes.io/aws-load-balancer-nlb-target-type": "instance",
                    "service.beta.kubernetes.io/aws-load-balancer-target-group-attributes": "preserve_client_ip.enabled=false",
                    "service.beta.kubernetes.io/aws-load-balancer-subnets": target_vpc.apply(
                        lambda tvpc: ",".join(tvpc["k8s_pod_subnet_ids"])
                    ),
                },
            },
        },
    ),
    opts=ResourceOptions(
        provider=k8s_provider,
        parent=operations_namespace,
        delete_before_replace=True,
        depends_on=[cluster, node_groups[0], operations_namespace, gateway_api_crds],
    ),
)
############################################################
# Configure external-dns operator to setup domain names automatically
############################################################
# Ref: https://github.com/kubernetes-sigs/external-dns
# Ref: https://github.com/kubernetes-sigs/external-dns/blob/master/docs/tutorials/aws.md
# Ref: https://github.com/kubernetes-sigs/external-dns/blob/master/docs/sources/traefik-proxy.md
external_dns_parliament_config = {
    "UNKNOWN_FEDERATION_SOURCE": {"ignore_locations": [{"principal": "federated"}]},
    "PERMISSIONS_MANAGEMENT_ACTIONS": {"ignore_locations": []},
    "MALFORMED": {"ignore_lcoations": []},
    "RESOURCE_STAR": {"ignore_lcoations": []},
}
external_dns_role = aws.iam.Role(
    f"{cluster_name}-external-dns-trust-role",
    name=f"{cluster_name}-external-dns-trust-role",
    path=f"/ol-infrastructure/eks/{cluster_name}/",
    assume_role_policy=cluster.eks_cluster.identities.apply(
        lambda ids: lint_iam_policy(
            oidc_trust_policy_template(
                oidc_identifier=ids[0]["oidcs"][0]["issuer"],
                account_id=aws_account.account_id,
                k8s_service_account_identifier="system:serviceaccount:operations:external-dns",
                operator="StringEquals",
            ),
            parliament_config=external_dns_parliament_config,
            stringify=True,
        )
    ),
    description="Trust role for allowing external-dns to modify route53 "
    "resources from within the cluster.",
    opts=ResourceOptions(parent=cluster, depends_on=cluster),
)

external_dns_policy_document = {
    "Version": IAM_POLICY_VERSION,
    "Statement": [
        {
            "Effect": "Allow",
            "Action": ["route53:ChangeResourceRecordSets"],
            "Resource": [
                # TODO @Ardiea interpolate with explicit zone IDs
                # More difficult than it sounds
                "arn:aws:route53:::hostedzone/*"
            ],
        },
        {
            "Effect": "Allow",
            "Action": [
                "route53:ListHostedZones",
                "route53:ListResourceRecordSets",
                "route53:ListTagsForResource",
            ],
            "Resource": ["*"],
        },
    ],
}
export("allowed_dns_zones", eks_config.require_object("allowed_dns_zones"))

external_dns_policy = aws.iam.Policy(
    f"{cluster_name}-external-dns-policy",
    name=f"{cluster_name}-external-dns-policy",
    path=f"/ol-infrastructure/eks/{cluster_name}/",
    policy=lint_iam_policy(
        external_dns_policy_document,
        parliament_config=external_dns_parliament_config,
        stringify=True,
    ),
    opts=ResourceOptions(parent=cluster, depends_on=cluster),
)
aws.iam.RolePolicyAttachment(
    f"{cluster_name}-external-dns-attachment",
    policy_arn=external_dns_policy.arn,
    role=external_dns_role.id,
    opts=ResourceOptions(parent=external_dns_role),
)
external_dns_release = (
    kubernetes.helm.v3.Release(
        f"{cluster_name}-external-dns-helm-release",
        kubernetes.helm.v3.ReleaseArgs(
            name="external-dns",
            chart="external-dns",
            version=VERSIONS["EXTERNAL_DNS_CHART"],
            namespace="operations",
            cleanup_on_fail=True,
            repository_opts=kubernetes.helm.v3.RepositoryOptsArgs(
                repo="https://kubernetes-sigs.github.io/external-dns/",
            ),
            values={
                "image": {
                    "pullPolicy": "Always",
                },
                "commonLabels": k8s_global_labels,
                "tolerations": operations_tolerations,
                "serviceAccount": {
                    "create": True,
                    "name": "external-dns",
                    "annotations": {
                        # Allows external-dns to make aws API calls to route53
                        "eks.amazonaws.com/role-arn": external_dns_role.arn.apply(
                            lambda arn: f"{arn}"
                        ),
                    },
                },
                "logLevel": "debug",
                "policy": "upsert-only",
                # Configure external-dns to only look at gateway resources
                # disables support for monitoring services or legacy ingress resources
                "sources": [
                    "gateway-udproute",
                    "gateway-tcproute",
                    "gateway-grpcroute",
                    "gateway-httproute",
                    "gateway-tlsroute",
                ],
                # Create a txt record to indicate provenance of the record(s)
                "txtOwnerId": cluster_name,
                # Need to explicitly turn off support for legacy traefik ingress services
                # to avoid an annoying bug
                "extraArgs": [
                    "--traefik-disable-legacy",
                ],
                # Limit the dns zones that exteranl dns knows about
                "domainFilters": eks_config.require_object("allowed_dns_zones"),
            },
        ),
        opts=ResourceOptions(
            provider=k8s_provider,
            parent=operations_namespace,
            delete_before_replace=True,
            depends_on=[cluster, node_groups[0], operations_namespace],
        ),
    ),
)

############################################################
# Install and configure cert-manager
############################################################
cert_manager_parliament_config = {
    "UNKNOWN_FEDERATION_SOURCE": {"ignore_locations": [{"principal": "federated"}]},
    "PERMISSIONS_MANAGEMENT_ACTIONS": {"ignore_locations": []},
    "MALFORMED": {"ignore_lcoations": []},
    "RESOURCE_STAR": {"ignore_lcoations": []},
}
# Cert manager uses DNS txt records to confirm that we control the
# domains that we are requesting certificates for.
# Ref: https://cert-manager.io/docs/configuration/acme/dns01/route53/#set-up-an-iam-role
cert_manager_role = aws.iam.Role(
    f"{cluster_name}-cert-manager-trust-role",
    name=f"{cluster_name}-cert-manager-trust-role",
    path=f"/ol-infrastructure/eks/{cluster_name}/",
    assume_role_policy=cluster.eks_cluster.identities.apply(
        lambda ids: lint_iam_policy(
            oidc_trust_policy_template(
                oidc_identifier=ids[0]["oidcs"][0]["issuer"],
                account_id=aws_account.account_id,
                k8s_service_account_identifier="system:serviceaccount:operations:cert-manager",
                operator="StringEquals",
            ),
            parliament_config=cert_manager_parliament_config,
            stringify=True,
        )
    ),
    description="Trust role for allowing cert-manager to modify route53 "
    "resources from within the cluster.",
    opts=ResourceOptions(parent=cluster, depends_on=cluster),
)
export("cert_manager_arn", cert_manager_role.arn)

cert_manager_policy_document = {
    "Version": IAM_POLICY_VERSION,
    "Statement": [
        {
            "Effect": "Allow",
            "Action": "route53:GetChange",
            "Resource": "arn:aws:route53:::change/*",
        },
        {
            "Effect": "Allow",
            "Action": [
                "route53:ChangeResourceRecordSets",
                "route53:ListResourceRecordSets",
            ],
            # TODO @Ardiea interpolate with explicit zone IDs
            # More difficult than it sounds
            "Resource": "arn:aws:route53:::hostedzone/*",
        },
        {"Effect": "Allow", "Action": "route53:ListHostedZonesByName", "Resource": "*"},
    ],
}

cert_manager_policy = aws.iam.Policy(
    f"{cluster_name}-cert-manager-policy",
    name=f"{cluster_name}-cert-manager-policy",
    path=f"/ol-infrastructure/eks/{cluster_name}/",
    policy=lint_iam_policy(
        cert_manager_policy_document,
        parliament_config=cert_manager_parliament_config,
        stringify=True,
    ),
    opts=ResourceOptions(parent=cluster, depends_on=cluster),
)
aws.iam.RolePolicyAttachment(
    f"{cluster_name}-cert-manager-attachment",
    policy_arn=cert_manager_policy.arn,
    role=cert_manager_role.id,
    opts=ResourceOptions(parent=cert_manager_role),
)

# Ref: https://cert-manager.io/docs/installation/
cert_manager_release = kubernetes.helm.v3.Release(
    f"{cluster_name}-cert-manager-helm-release",
    kubernetes.helm.v3.ReleaseArgs(
        name="cert-manager",
        chart="cert-manager",
        version=VERSIONS["CERT_MANAGER_CHART"],
        namespace="operations",
        repository_opts=kubernetes.helm.v3.RepositoryOptsArgs(
            repo="https://charts.jetstack.io",
        ),
        cleanup_on_fail=True,
        skip_await=False,
        values={
            "crds": {
                "enabled": True,
                "keep": True,
            },
            "global": {
                "commonLabels": k8s_global_labels,
            },
            "replicaCount": 1,
            "enableCertificateOwnerRef": True,
            "prometheus": {
                "enabled": False,
            },
            "config": {
                "apiVersion": "controller.config.cert-manager.io/v1alpha1",
                "kind": "ControllerConfiguration",
                "enableGatewayAPI": True,
            },
            "serviceAccount": {
                "create": True,
                "name": "cert-manager",
                "annotations": {
                    # Allows cert-manager to make aws API calls to route53
                    "eks.amazonaws.com/role-arn": cert_manager_role.arn.apply(
                        lambda arn: f"{arn}"
                    ),
                },
            },
        },
    ),
    opts=ResourceOptions(
        provider=k8s_provider,
        parent=operations_namespace,
        depends_on=[cluster, node_groups[0]],
        delete_before_replace=True,
    ),
)

export(
    "kube_config_data",
    {
        "role_arn": administrator_role.arn,
        "certificate-authority-data": cluster.eks_cluster.certificate_authority,
        "server": cluster.eks_cluster.endpoint,
    },
)
