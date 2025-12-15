import os
from pathlib import Path

import pulumi_aws as aws
import pulumi_kubernetes as kubernetes
import pulumi_vault as vault
from pulumi import Config, ResourceOptions, StackReference

from bridge.lib.versions import (
    GRAFANA_K8S_MONITORING_CHART_VERSION,
    NVIDIA_DCGM_EXPORTER_CHART_VERSION,
    NVIDIA_K8S_DEVICE_PLUGIN_CHART_VERSION,
    VANTAGE_K8S_AGENT_CHART_VERSION,
)
from ol_infrastructure.components.services.vault import (
    OLVaultK8SResources,
    OLVaultK8SResourcesConfig,
    OLVaultK8SSecret,
    OLVaultK8SStaticSecretConfig,
)
from ol_infrastructure.lib.ol_types import AWSBase
from ol_infrastructure.lib.pulumi_helper import parse_stack
from ol_infrastructure.lib.vault import setup_vault_provider
from ol_infrastructure.substructure.aws.eks.grafana import setup_grafana
from ol_infrastructure.substructure.aws.eks.karpenter import setup_karpenter
from ol_infrastructure.substructure.aws.eks.keda import setup_keda
from ol_infrastructure.substructure.aws.eks.nvidia import setup_nvidia

env_config = Config("environment")

aws_account = aws.get_caller_identity()

VERSIONS = {
    "VANTAGE_K8S_AGENT_VERSION": os.environ.get(
        "VANTAGE_K8S_AGENT_CHART_VERSION", VANTAGE_K8S_AGENT_CHART_VERSION
    ),
    "GRAFANA_K8S_MONITORING_VERSION": os.environ.get(
        "GRAFANA_K8S_MONITORING_CHART_VERSION", GRAFANA_K8S_MONITORING_CHART_VERSION
    ),
    "NVIDIA_DCGM_EXPORTER_VERSION": os.environ.get(
        "NVIDIA_DCGM_EXPORTER_VERSION", NVIDIA_DCGM_EXPORTER_CHART_VERSION
    ),
    "NVIDIA_K8S_DEVICE_PLUGIN_VERSION": os.environ.get(
        "NVIDIA_K8S_DEVICE_PLUGIN_VERSION", NVIDIA_K8S_DEVICE_PLUGIN_CHART_VERSION
    ),
}

stack_info = parse_stack()

cluster_stack = StackReference(
    f"infrastructure.aws.eks.{stack_info.env_prefix}.{stack_info.name}"
)
kms_stack = StackReference(f"infrastructure.aws.kms.{stack_info.name}")
network_stack = StackReference(f"infrastructure.aws.network.{stack_info.name}")

target_vpc = network_stack.require_output(env_config.require("target_vpc"))

cluster_name = f"{stack_info.env_prefix}-{stack_info.env_suffix}"

aws_config = AWSBase(
    tags={
        "OU": env_config.get("business_unit") or "operations",
        "Environment": cluster_name,
        "Owner": "platform-engineering",
    },
)

k8s_global_labels = {
    "pulumi_managed": "true",
    "pulumi_stack": stack_info.full_name,
    "ol.mit.edu/stack": stack_info.full_name,
}

setup_vault_provider(stack_info)
k8s_provider = kubernetes.Provider(
    "k8s-provider",
    kubeconfig=cluster_stack.require_output("kube_config"),
)

############################################################
# Secondary resources for cert-manager
############################################################

# ClusterIssuer resources to provide a shared, preconfigured method
# for requesting certificates from letsencrypt
cert_manager_clusterissuer_resources = kubernetes.yaml.v2.ConfigGroup(
    f"{cluster_name}-cert-manager-clusterissuer-resources",
    skip_await=True,
    objs=[
        {
            "apiVersion": "cert-manager.io/v1",
            "kind": "ClusterIssuer",
            "metadata": {
                "name": "letsencrypt-staging",
                "labels": k8s_global_labels,
            },
            "spec": {
                "acme": {
                    "email": "odl-devops@mit.edu",
                    "server": "https://acme-staging-v02.api.letsencrypt.org/directory",
                    "disableAccountKeyGeneration": False,
                    "privateKeySecretRef": {
                        "name": "letsencrypt-staging-private-key",
                    },
                    "solvers": [
                        {
                            "selector": {
                                "dnsZones": cluster_stack.require_output(
                                    "allowed_dns_zones"
                                ),
                            },
                            "dns01": {
                                "route53": {},
                            },
                        },
                    ],
                },
            },
        },
        {
            "apiVersion": "cert-manager.io/v1",
            "kind": "ClusterIssuer",
            "metadata": {
                "name": "letsencrypt-production",
                "labels": k8s_global_labels,
            },
            "spec": {
                "acme": {
                    "email": "odl-devops@mit.edu",
                    "server": "https://acme-v02.api.letsencrypt.org/directory",
                    "disableAccountKeyGeneration": False,
                    "privateKeySecretRef": {
                        "name": "letsencrypt-production-private-key",
                    },
                    "solvers": [
                        {
                            "selector": {
                                "dnsZones": cluster_stack.require_output(
                                    "allowed_dns_zones"
                                ),
                            },
                            "dns01": {
                                "route53": {},
                            },
                        },
                    ],
                },
            },
        },
    ],
    opts=ResourceOptions(
        provider=k8s_provider,
        delete_before_replace=True,
    ),
)

############################################################
# Install the vantage k8s agent
############################################################
# Requires EBS storage class and creates a statefulset
if cluster_stack.require_output("has_ebs_storage"):
    vault_vantage_policy_name = f"{stack_info.env_prefix}-eks-vantage"
    vault_vantage_policy = vault.Policy(
        f"{cluster_name}-eks-vault-vantage-policy",
        name=vault_vantage_policy_name,
        policy=Path(__file__).parent.joinpath("vantage_vault_policy.hcl").read_text(),
    )
    vault_vantage_auth_backend_role = vault.kubernetes.AuthBackendRole(
        f"{cluster_name}-vantage-agent-vault-auth-backend-role",
        role_name="vantage-agent",
        backend=cluster_stack.require_output("vault_auth_endpoint"),
        bound_service_account_names=["*"],
        bound_service_account_namespaces=["operations"],
        token_policies=[vault_vantage_policy_name],
    )

    vault_vantage_k8s_resources_config = OLVaultK8SResourcesConfig(
        application_name="vantage-agent",
        namespace="operations",
        labels=k8s_global_labels,
        vault_address=f"https://vault-{stack_info.env_suffix}.odl.mit.edu",
        vault_auth_endpoint=cluster_stack.require_output("vault_auth_endpoint"),
        vault_auth_role_name=vault_vantage_auth_backend_role.role_name,
    )

    vault_vantage_k8s_resources = OLVaultK8SResources(
        resource_config=vault_vantage_k8s_resources_config,
        opts=ResourceOptions(
            provider=k8s_provider,
            delete_before_replace=True,
            depends_on=[vault_vantage_auth_backend_role],
        ),
    )
    vantage_api_token_secret_name = "vantage-api-token"  # noqa: S105  # pragma: allowlist secret
    vantage_api_token_secret_config = OLVaultK8SStaticSecretConfig(
        name="vault-kv-global-vantage-api-token",
        namespace="operations",
        labels=k8s_global_labels,
        dest_secret_labels=k8s_global_labels,
        dest_secret_name=vantage_api_token_secret_name,
        mount="secret-global",
        mount_type="kv-v2",
        path="vantage",
        templates={
            "token": '{{ get .Secrets "token" }}',
        },
        refresh_after="1h",
        vaultauth=vault_vantage_k8s_resources.auth_name,
    )

    vantage_api_token_secret = OLVaultK8SSecret(
        f"{cluster_name}-vantage-api-token-static-secret",
        resource_config=vantage_api_token_secret_config,
        opts=ResourceOptions(
            provider=k8s_provider,
            delete_before_replace=True,
        ),
    )

    vantage_k8s_agent_release = kubernetes.helm.v3.Release(
        f"{cluster_name}-vantage-k8s-agent-helm-release",
        kubernetes.helm.v3.ReleaseArgs(
            name="vantage-kubernetes-agent",
            chart="vantage-kubernetes-agent",
            version=VERSIONS["VANTAGE_K8S_AGENT_VERSION"],
            namespace="operations",
            cleanup_on_fail=True,
            repository_opts=kubernetes.helm.v3.RepositoryOptsArgs(
                repo="https://vantage-sh.github.io/helm-charts",
            ),
            values={
                "agent": {
                    "secret": {
                        "name": vantage_api_token_secret_name,
                        "key": "token",
                    },
                    "clusterID": cluster_name,
                    "disableKubeTLSverify": True,
                    "nodeAddressTypes": "InternalIP",
                    "collectNamespaceLabels": "true",
                },
                "persist": {
                    "storageClassName": cluster_stack.require_output(
                        "ebs_storageclass"
                    ),
                },
                "resources": {
                    "requests": {
                        "cpu": "10m",
                        "memory": "200Mi",
                    },
                    "limits": {
                        "memory": "200Mi",
                    },
                },
            },
            skip_await=True,
        ),
        opts=ResourceOptions(
            provider=k8s_provider,
            depends_on=[vantage_api_token_secret],
        ),
    )

# Setup Grafana k8s-monitoring
setup_grafana(
    cluster_name=cluster_name,
    stack_info=stack_info,
    k8s_provider=k8s_provider,
    grafana_k8s_monitoring_version=VERSIONS["GRAFANA_K8S_MONITORING_VERSION"],
)

# Setup Karpenter
setup_karpenter(
    cluster_name=cluster_name,
    cluster_stack=cluster_stack,
    kms_stack=kms_stack,
    aws_config=aws_config,
    k8s_provider=k8s_provider,
    aws_account=aws_account,
    k8s_global_labels=k8s_global_labels,
)

############################################################
# KEDA (Kubernetes Event Driven Autoscaling)
############################################################
setup_keda(
    cluster_name=cluster_name,
    cluster_stack=cluster_stack,
    target_vpc=target_vpc,
    aws_config=aws_config,
    k8s_provider=k8s_provider,
    k8s_global_labels=k8s_global_labels,
)

# Setup NVIDIA GPU resources
setup_nvidia(
    cluster_name=cluster_name,
    k8s_provider=k8s_provider,
    nvidia_dcgm_exporter_version=VERSIONS["NVIDIA_DCGM_EXPORTER_VERSION"],
    nvidia_k8s_device_plugin_version=VERSIONS["NVIDIA_K8S_DEVICE_PLUGIN_VERSION"],
)
