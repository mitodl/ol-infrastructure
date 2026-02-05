import pulumi_kubernetes as kubernetes
from pulumi import Config, ResourceOptions, StackReference
from pulumi_aws import get_caller_identity, iam

from bridge.lib.versions import STARROCKS_OPERATOR_CHART_VERSION
from ol_infrastructure.components.aws.eks import (
    OLEKSTrustRole,
    OLEKSTrustRoleConfig,
)
from ol_infrastructure.lib.aws.eks_helper import check_cluster_namespace
from ol_infrastructure.lib.ol_types import AWSBase
from ol_infrastructure.lib.pulumi_helper import StackInfo


def setup_starrocks(
    cluster_name: str,
    cluster_stack: StackReference,
    k8s_provider: kubernetes.Provider,
    stack_info: StackInfo,
    aws_config: AWSBase,
):
    """
    Set up StarRocks operator resources including Helm chart installation.

    Only installs if starrocks.enable_operator is set to true in configuration.

    Args:
        cluster_name: The name of the EKS cluster.
        cluster_stack: A StackReference to the EKS cluster stack.
        k8s_provider: The Pulumi Kubernetes provider instance.
        stack_info: Information about the current Pulumi stack, including the stack
            name used to construct related stack references.
        aws_config: AWS account configuration, including common tags and other
            AWS-related metadata to apply to created resources.
    """
    aws_account = get_caller_identity()

    data_warehouse_stack = StackReference(
        f"infrastructure.aws.data_warehouse.{stack_info.name}"
    )
    data_lake_query_engine_iam_policy_arn = data_warehouse_stack.require_output(
        "data_lake_query_engine_iam_policy_arn",
    )

    starrocks_config = Config("starrocks")
    if not starrocks_config.get_bool("enable_operator"):
        return

    starrocks_namespace = "starrocks"
    cluster_stack.require_output("namespaces").apply(
        lambda ns: check_cluster_namespace(starrocks_namespace, ns)
    )

    starrocks_trust_role_config = OLEKSTrustRoleConfig(
        account_id=aws_account.account_id,
        cluster_name=f"data-{stack_info.name}",
        cluster_identities=cluster_stack.require_output("cluster_identities"),
        description="Trust role for allowing the starrocks service account to "
        "access the aws API",
        policy_operator="StringEquals",
        role_name="starrocks",
        service_account_identifier=f"system:serviceaccount:{starrocks_namespace}:starrocks",
        tags=aws_config.tags,
    )

    starrocks_trust_role = OLEKSTrustRole(
        f"{cluster_name}-starrocks-ol-trust-role",
        role_config=starrocks_trust_role_config,
    )

    iam.RolePolicyAttachment(
        f"{cluster_name}-starrocks-data-lake-access-policy-attachment",
        policy_arn=data_lake_query_engine_iam_policy_arn,
        role=starrocks_trust_role.role.name,
    )

    kubernetes.helm.v3.Release(
        f"{cluster_name}-starrocks-operator-helm-release",
        kubernetes.helm.v3.ReleaseArgs(
            name="starrocks-operator",
            chart="operator",
            version=STARROCKS_OPERATOR_CHART_VERSION,
            namespace=starrocks_namespace,
            cleanup_on_fail=True,
            skip_await=False,
            repository_opts=kubernetes.helm.v3.RepositoryOptsArgs(
                repo="https://starrocks.github.io/starrocks-kubernetes-operator",
            ),
            values={
                "global": {
                    "rbac": {
                        "create": True,
                        "serviceAccount": {
                            "annotations": {
                                "eks.amazonaws.com/role-arn": starrocks_trust_role.role.arn,  # noqa: E501
                            },
                        },
                    },
                },
                "timeZone": "UTC",
                "nameOverride": "starrocks-operator",
                "starrocksOperator": {
                    "enabled": True,
                    "imagePullPolicy": "IfNotPresent",
                    "replicaCount": 1,
                    "resources": {
                        "requests": {
                            "cpu": "10m",
                            "memory": "128Mi",
                        },
                        "limits": {
                            "memory": "128Mi",
                        },
                    },
                },
            },
        ),
        opts=ResourceOptions(provider=k8s_provider, delete_before_replace=True),
    )
