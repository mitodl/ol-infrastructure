import pulumi_kubernetes as kubernetes
from pulumi import Config, ResourceOptions, StackReference, export
from pulumi_aws import get_caller_identity, iam

from bridge.lib.versions import STARROCKS_OPERATOR_CHART_VERSION
from ol_infrastructure.components.aws.eks import (
    OLEKSTrustRole,
    OLEKSTrustRoleConfig,
)
from ol_infrastructure.lib import pulumi_projects as projects
from ol_infrastructure.lib.aws.eks_helper import check_cluster_namespace
from ol_infrastructure.lib.aws.iam_helper import cross_environment_glue_denial
from ol_infrastructure.lib.ol_types import AWSBase
from ol_infrastructure.lib.pulumi_helper import (
    StackInfo,
    make_stack_reference,
)


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

    data_warehouse_stack = make_stack_reference(
        projects.DATA_WAREHOUSE, stack_info.name
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

    # Scoped to this role rather than folded into the managed policy above, which
    # is shared across environments and so cannot hold a Deny that applies to only
    # one of them. Attached as a managed policy because the Concourse deploy role
    # has iam:AttachRolePolicy but not iam:PutRolePolicy. Empty in production.
    # Defence in depth: the policy's Allow is already scoped to this environment.
    if cross_environment_glue_denial(stack_info.env_suffix):
        iam.RolePolicyAttachment(
            f"{cluster_name}-starrocks-cross-environment-glue-denial",
            policy_arn=data_warehouse_stack.require_output(
                "data_lake_cross_environment_glue_denial_policy_arn"
            ),
            role=starrocks_trust_role.role.name,
        )

    # skip_await=False (the default) makes this resource block until Helm confirms
    # the operator is actually deployed. The applications/starrocks stack requires
    # this export before installing the FE/CN chart, so that stack fails hard
    # instead of racing ahead of the operator — the FE/CN chart's initPassword
    # hook silently no-ops if the operator (and thus the FE) doesn't exist yet,
    # leaving the FE root user permanently out of sync with the k8s secret.
    starrocks_operator_release = kubernetes.helm.v3.Release(
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
                            # The chart's default SA name ("starrocks") collides
                            # with the ServiceAccount that applications/starrocks
                            # creates directly via Pulumi for FE/CN/BE pod IRSA in
                            # the same namespace. Helm refuses to adopt a resource
                            # it didn't create, so the operator's own pod identity
                            # needs a distinct name.
                            "name": "starrocks-operator",
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
                    # The operator's controller-runtime cache is cluster-wide by
                    # default, so its memory tracks total pods in the cluster
                    # rather than anything about StarRocks. data-production
                    # retains ~4k completed dagster Job pods, which pushed it
                    # past the old 512Mi ceiling and left the Deployment
                    # unavailable. Only limits.memory is set here; the chart
                    # default supplies limits.cpu (500m) via Helm's deep merge,
                    # and the requests below deliberately undercut the chart's
                    # 500m/400Mi so the operator does not reserve capacity it
                    # never uses at rest.
                    "resources": {
                        "requests": {
                            "cpu": "10m",
                            "memory": "256Mi",
                        },
                        "limits": {
                            "memory": "1Gi",
                        },
                    },
                },
            },
        ),
        opts=ResourceOptions(provider=k8s_provider, delete_before_replace=True),
    )

    export("starrocks_operator_status", starrocks_operator_release.status.status)
