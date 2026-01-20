"""Deploy StarRocks data warehouse to EKS with Pulumi and Helm.

This module installs the StarRocks operator, configures IAM roles and trust
relationships for the StarRocks service account, manages Kubernetes secrets
(including the root password), and provisions the StarRocks cluster
configuration on the target EKS cluster.
"""
# ruff: noqa: E501

from typing import Any

import pulumi
import pulumi_kubernetes as kubernetes
from pulumi import Config, StackReference, export
from pulumi_aws import get_caller_identity, iam

from bridge.lib.versions import STARROCKS_OPERATOR_CHART_VERSION, STARROCKS_VERSION
from ol_infrastructure.components.aws.eks import OLEKSTrustRole, OLEKSTrustRoleConfig
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

stack_info = parse_stack()
starrocks_config = Config("starrocks")
use_shared_data = starrocks_config.get_bool("shared_data") or False

cluster_stack = StackReference(f"infrastructure.aws.eks.data.{stack_info.name}")
setup_k8s_provider(kubeconfig=cluster_stack.require_output("kube_config"))

data_warehouse_stack = StackReference(
    f"infrastructure.aws.data_warehouse.{stack_info.name}"
)

aws_account = get_caller_identity()
aws_config = AWSBase(
    tags={"OU": "operations", "Environment": f"data-{stack_info.env_suffix}"},
)

namespace = "starrocks"
cluster_stack.require_output("namespaces").apply(
    lambda ns: check_cluster_namespace(namespace, ns)
)

k8s_global_labels = K8sGlobalLabels(
    service=Services.starrocks,
    ou=BusinessUnit.data,
    stack=stack_info,
).model_dump()

starrocks_serviceaccount_name = "starrocks"
starrocks_trust_role_config = OLEKSTrustRoleConfig(
    account_id=aws_account.account_id,
    cluster_name=f"data-{stack_info.name}",
    cluster_identities=cluster_stack.require_output("cluster_identities"),
    description="Trust role for allowing the starrocks service account to "
    "access the AWS API",
    policy_operator="StringEquals",
    role_name="starrocks",
    service_account_identifier=f"system:serviceaccount:{namespace}:{starrocks_serviceaccount_name}",
    tags=aws_config.tags,
)
starrocks_trust_role = OLEKSTrustRole(
    f"starrocks-trust-role-{stack_info.env_suffix}",
    role_config=starrocks_trust_role_config,
)
iam.RolePolicyAttachment(
    f"starrocks-trust-role-attachment-{stack_info.env_suffix}",
    policy_arn=data_warehouse_stack.require_output(
        "data_lake_query_engine_iam_policy_arn"
    ),
    role=starrocks_trust_role.role.name,
)
export("starrocks_iam_role_arn", starrocks_trust_role.role.arn)

# Create secret for StarRocks password
starrocks_password_secret = kubernetes.core.v1.Secret(
    "starrocks-root-password",
    metadata=kubernetes.meta.v1.ObjectMetaArgs(
        name="starrocks-root-password",
        namespace=namespace,
        labels=k8s_global_labels,
    ),
    string_data={
        "password": starrocks_config.require_secret("root_password"),
    },
)

kube_starrocks_helm_values: dict[str, Any] = {
    "operator": {
        "timeZone": "UTC",
        "nameOverride": "kube-starrocks",
        "global": {
            "rbac": {
                "create": True,
                "serviceAccount": {
                    "name": starrocks_serviceaccount_name,
                    "annotations": {
                        "eks.amazonaws.com/role-arn": starrocks_trust_role.role.arn,
                    },
                    "labels": {},
                },
            },
        },
        "starrocksOperator": {
            "enabled": True,
            "image": {
                "repository": "starrocks/operator",
                "tag": "v1.11.3",
            },
            "imagePullPolicy": "Always",
            "resources": {
                "requests": {"cpu": "500m", "memory": "400Mi"},
                "limits": {"cpu": "500m", "memory": "800Mi"},
            },
        },
    },
    "starrocks": {
        "timeZone": "UTC",
        "nameOverride": "kube-starrocks",
        # Configure root password from secret
        "initPassword": {
            "enabled": True,
            "isInstall": True,
            "passwordSecret": "starrocks-root-password",  # pragma: allowlist secret
        },
        # Disable external integrations
        "datadog": {
            "log": {"enabled": False},
            "metrics": {"enabled": False},
            "profiling": {"fe": False, "be": False, "cn": False},
        },
        "metrics": {
            "serviceMonitor": {"enabled": False},
        },
        # Cluster configuration - use CN for shared-data, BE for shared-nothing
        "starrocksCluster": {
            "name": "ol-starrocks",
            "namespace": namespace,
            "enabledBe": not use_shared_data,  # BE only for shared-nothing architecture
            "enabledCn": use_shared_data,  # CN only for shared-data architecture
        },
        # Frontend (FE) - Query coordinator and metadata service
        "starrocksFESpec": {
            "replicas": 3,
            "image": {"repository": "starrocks/fe-ubuntu", "tag": STARROCKS_VERSION},
            "imagePullPolicy": "IfNotPresent",
            "runAsNonRoot": False,
            "service": {
                "type": "ClusterIP",
                "annotations": {},
            },
            "resources": {
                "requests": {"cpu": 2, "memory": "10Gi"},
                "limits": {"memory": "10Gi"},
            },
            "storageSpec": {
                "name": "fe",
                "storageSize": "10Gi",
                "logStorageSize": "5Gi",
            },
            "config": """LOG_DIR = ${STARROCKS_HOME}/log
DATE = "$(date +%Y%m%d-%H%M%S)"
JAVA_OPTS="-Dlog4j2.formatMsgNoLookups=true -Xmx8192m -XX:+UseG1GC -Xlog:gc*:${LOG_DIR}/fe.gc.log.$DATE:time"
http_port = 8030
rpc_port = 9020
query_port = 9030
edit_log_port = 9010
mysql_service_nio_enabled = true
sys_log_level = INFO
""",
        },
        # Backend (BE) - Storage and compute nodes
        # Only relevant if shared_data == false
        "starrocksBeSpec": {
            "replicas": 3,
            "image": {
                "repository": "starrocks/be-ubuntu",
                "tag": STARROCKS_VERSION,
            },
            "imagePullPolicy": "IfNotPresent",
            "runAsNonRoot": False,
            "service": {
                "type": "ClusterIP",
                "annotations": {},
            },
            "resources": {
                "requests": {"cpu": 2, "memory": "12Gi"},
                "limits": {"memory": "12Gi"},
            },
            "storageSpec": {
                "name": "be",
                "storageSize": "100Gi",
                "logStorageSize": "5Gi",
            },
            "config": """sys_log_level = INFO
be_port = 9060
be_http_port = 8040
heartbeat_service_port = 9050
brpc_port = 8060
""",
        },
        # Compute Node (CN) - Stateless compute nodes for shared-data architecture
        # Only relevant if shared_data == true
        "starrocksCnSpec": {
            "replicas": 3,
            "image": {
                "repository": "starrocks/cn-ubuntu",
                "tag": STARROCKS_VERSION,
            },
            "imagePullPolicy": "IfNotPresent",
            "runAsNonRoot": False,
            "service": {
                "type": "ClusterIP",
                "annotations": {},
            },
            "resources": {
                "requests": {"cpu": 2, "memory": "10Gi"},
                "limits": {"memory": "10Gi"},
            },
            "storageSpec": {
                "name": "cn",
                # In shared-data mode, CNs don't store data, only logs and cache
                "storageSize": "50Gi",  # For local cache
                "logStorageSize": "5Gi",
            },
            "config": """sys_log_level = INFO
thrift_port = 9060
webserver_port = 8040
heartbeat_service_port = 9050
brpc_port = 8060
""",
        },
    },
}

kube_starrocks_release = kubernetes.helm.v3.Release(
    "kube-starrocks",
    kubernetes.helm.v3.ReleaseArgs(
        name="kube-starrocks",
        chart="kube-starrocks",
        version=STARROCKS_OPERATOR_CHART_VERSION,
        namespace=namespace,
        cleanup_on_fail=True,
        skip_await=False,
        repository_opts=kubernetes.helm.v3.RepositoryOptsArgs(
            repo="https://starrocks.github.io/starrocks-kubernetes-operator",
        ),
        values=kube_starrocks_helm_values,
    ),
    opts=pulumi.ResourceOptions(depends_on=[starrocks_password_secret]),
)
