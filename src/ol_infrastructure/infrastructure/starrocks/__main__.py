"""Deploys the StarRocks application to an EKS cluster."""

import pulumi
import pulumi_kubernetes as k8s
from pulumi_kubernetes.core.v1 import Namespace
from pulumi_kubernetes.yaml import ConfigFile

from ol_infrastructure.infrastructure.starrocks.models import StarRocksConfig
from ol_infrastructure.lib import pulumi_projects as projects
from ol_infrastructure.lib.pulumi_helper import parse_stack, stack_ref

stack_info = parse_stack()
starrocks_config = StarRocksConfig.model_validate(
    pulumi.Config("starrocks").get_object("config")
)

# Assumes the EKS cluster stack is named infrastructure.aws.eks.<cluster>.<stack_name>
kubeconfig = pulumi.StackReference(
    stack_ref(projects.EKS, f"{starrocks_config.eks_cluster_name}.{stack_info.name}")
).require_output("kube_config")

k8s_provider = k8s.Provider(
    "starrocks-k8s-provider",
    kubeconfig=kubeconfig,
    enable_server_side_apply=True,
)

# Deploy StarRocks Operator CRD
starrocks_crd = ConfigFile(
    "starrocks-crd",
    file=starrocks_config.operator_crd_url,
    opts=pulumi.ResourceOptions(provider=k8s_provider),
)

# Deploy StarRocks Operator
starrocks_operator = ConfigFile(
    "starrocks-operator",
    file=starrocks_config.operator_deploy_url,
    opts=pulumi.ResourceOptions(provider=k8s_provider, depends_on=[starrocks_crd]),
)

# Create the namespace for the StarRocks cluster
starrocks_namespace = Namespace(
    "starrocks-namespace",
    metadata={"name": starrocks_config.namespace},
    opts=pulumi.ResourceOptions(provider=k8s_provider),
)

# Deploy the StarRocks cluster
starrocks_cluster = k8s.apiextensions.CustomResource(
    "starrocks-cluster",
    api_version="starrocks.com/v1",
    kind="StarRocksCluster",
    metadata={
        "name": f"starrocks-{stack_info.env_suffix}",
        "namespace": starrocks_namespace.metadata["name"],
    },
    spec=starrocks_config.cluster_definition.model_dump(by_alias=True),
    opts=pulumi.ResourceOptions(provider=k8s_provider, depends_on=[starrocks_operator]),
)
