# ruff: noqa: E501

import os
from pathlib import Path

import pulumi_aws as aws
import pulumi_kubernetes as kubernetes
import pulumi_vault as vault
from pulumi import Config, ResourceOptions, StackReference, export

from bridge.lib.versions import (
    GRAFANA_K8S_MONITORING_CHART_VERSION,
    NVIDIA_DCGM_EXPORTER_CHART_VERSION,
    NVIDIA_K8S_DEVICE_PLUGIN_CHART_VERSION,
    VANTAGE_K8S_AGENT_CHART_VERSION,
)
from bridge.secrets.sops import read_yaml_secrets
from ol_infrastructure.components.services.vault import (
    OLVaultK8SResources,
    OLVaultK8SResourcesConfig,
    OLVaultK8SSecret,
    OLVaultK8SStaticSecretConfig,
)
from ol_infrastructure.lib.ol_types import AWSBase
from ol_infrastructure.lib.pulumi_helper import parse_stack
from ol_infrastructure.lib.vault import setup_vault_provider
from ol_infrastructure.substructure.aws.eks.karpenter import setup_karpenter
from ol_infrastructure.substructure.aws.eks.keda import setup_keda

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
# Secondary resources for vault-secrets-operator
############################################################
vault_traefik_policy_name = f"{stack_info.env_prefix}-eks-traefik"
vault_traefik_policy = vault.Policy(
    f"{cluster_name}-eks-vault-traefik-policy",
    name=vault_traefik_policy_name,
    policy=Path(__file__).parent.joinpath("operations_vault_policy.hcl").read_text(),
)
vault_traefik_auth_backend_role = vault.kubernetes.AuthBackendRole(
    f"{cluster_name}-traefik-gateway-vault-auth-backend-role",
    role_name="traefik-gateway",
    backend=cluster_stack.require_output("vault_auth_endpoint"),
    bound_service_account_names=["*"],
    bound_service_account_namespaces=["operations"],
    token_policies=[vault_traefik_policy_name],
)

operations_vault_k8s_resources_config = OLVaultK8SResourcesConfig(
    application_name="operations",
    namespace="operations",
    labels=k8s_global_labels,
    vault_address=f"https://vault-{stack_info.env_suffix}.odl.mit.edu",
    vault_auth_endpoint=cluster_stack.require_output("vault_auth_endpoint"),
    vault_auth_role_name=vault_traefik_auth_backend_role.role_name,
)

operations_vault_k8s_resources = OLVaultK8SResources(
    resource_config=operations_vault_k8s_resources_config,
    opts=ResourceOptions(
        provider=k8s_provider,
        delete_before_replace=True,
    ),
)
star_odl_mit_edu_secret_name = (
    "odl-wildcard-cert"  # pragma: allowlist secret #  noqa: S105
)
star_odl_mit_edu_static_secret_config = OLVaultK8SStaticSecretConfig(
    name="vault-kv-global-odl-wildcard",
    namespace="operations",
    labels=k8s_global_labels,
    dest_secret_labels=k8s_global_labels,
    dest_secret_name=star_odl_mit_edu_secret_name,
    dest_secret_type="kubernetes.io/tls",  # noqa: S106  # pragma: allowlist secret
    mount="secret-global",
    mount_type="kv-v2",
    path="odl-wildcard",
    templates={
        "tls.key": '{{ get .Secrets "key_with_proper_newlines" }}',
        "tls.crt": '{{ get .Secrets "cert_with_proper_newlines" }}',
        # Ref: https://apisix.apache.org/docs/ingress-controller/concepts/apisix_tls/
        "key": '{{ get .Secrets "key_with_proper_newlines" }}',
        "cert": '{{ get .Secrets "cert_with_proper_newlines" }}',
    },
    refresh_after="1h",
    vaultauth=operations_vault_k8s_resources.auth_name,
)
star_odl_mit_edu_static_secret = OLVaultK8SSecret(
    f"{cluster_name}-odl-mit-edu-wildcard-static-secret",
    resource_config=star_odl_mit_edu_static_secret_config,
    opts=ResourceOptions(
        provider=k8s_provider,
        delete_before_replace=True,
    ),
)
export("star_odl_mit_edu_secret_name", star_odl_mit_edu_secret_name)
export("star_odl_mit_edu_secret_namespace", "operations")

star_ol_mit_edu_secret_name = (
    "ol-wildcard-cert"  # pragma: allowlist secret #  noqa: S105
)
star_ol_mit_edu_static_secret_config = OLVaultK8SStaticSecretConfig(
    name="vault-kv-global-ol-wildcard",
    namespace="operations",
    labels=k8s_global_labels,
    dest_secret_labels=k8s_global_labels,
    dest_secret_name=star_ol_mit_edu_secret_name,
    dest_secret_type="kubernetes.io/tls",  # noqa: S106  # pragma: allowlist secret
    mount="secret-global",
    mount_type="kv-v2",
    path="ol-wildcard",
    templates={
        "tls.key": '{{ get .Secrets "key_with_proper_newlines" }}',
        "tls.crt": '{{ get .Secrets "cert_with_proper_newlines" }}',
        # Ref: https://apisix.apache.org/docs/ingress-controller/concepts/apisix_tls/
        "key": '{{ get .Secrets "key_with_proper_newlines" }}',
        "cert": '{{ get .Secrets "cert_with_proper_newlines" }}',
    },
    refresh_after="1h",
    vaultauth=operations_vault_k8s_resources.auth_name,
)
star_ol_mit_edu_static_secret = OLVaultK8SSecret(
    f"{cluster_name}-ol-mit-edu-wildcard-static-secret",
    resource_config=star_ol_mit_edu_static_secret_config,
    opts=ResourceOptions(
        provider=k8s_provider,
        delete_before_replace=True,
    ),
)
export("star_ol_mit_edu_secret_name", star_ol_mit_edu_secret_name)
export("star_ol_mit_edu_secret_namespace", "operations")


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

# Grafana k8s-monitoring
grafana_vault_secrets = read_yaml_secrets(
    Path(f"alloy/grafana.{stack_info.env_suffix}.yaml")
)

alloy_extra_env_vars = [
    {
        "name": "GCLOUD_RW_API_KEY",
        "valueFrom": {
            "secretKeyRef": {
                "name": "alloy-metrics-remote-cfg-grafana-k8s-monitoring",
                "key": "password",
            }
        },
    },
    {
        "name": "CLUSTER_NAME",
        "value": cluster_name,
    },
    {
        "name": "NAMESPACE",
        "valueFrom": {
            "fieldRef": {"fieldPath": "metadata.namespace"},
        },
    },
    {
        "name": "POD_NAME",
        "valueFrom": {
            "fieldRef": {"fieldPath": "metadata.name"},
        },
    },
    {
        "name": "GCLOUD_FM_COLLECTOR_ID",
        "value": "grafana-k8s-monitoring-$(CLUSTER_NAME)-$(NAMESPACE)-$(POD_NAME)",
    },
]
grafana_k8s_monitoring_helm_release = kubernetes.helm.v3.Release(
    f"{cluster_name}-grafana-k8s-monitoring-helm-release",
    kubernetes.helm.v3.ReleaseArgs(
        name="grafana-k8s-monitoring",
        chart="k8s-monitoring",
        version=VERSIONS["GRAFANA_K8S_MONITORING_VERSION"],
        namespace="grafana",
        create_namespace=True,  # Important
        cleanup_on_fail=True,
        repository_opts=kubernetes.helm.v3.RepositoryOptsArgs(
            repo="https://grafana.github.io/helm-charts",
        ),
        values={
            "cluster": {
                "name": cluster_name,
            },
            "destinations": [
                {
                    "name": "grafana-cloud-metrics",
                    "type": "prometheus",
                    "url": "https://prometheus-prod-10-prod-us-central-0.grafana.net./api/prom/push",
                    "auth": {
                        "type": "basic",
                        "username": grafana_vault_secrets[
                            "k8s_monitoring_metrics_username"
                        ],
                        "password": grafana_vault_secrets["k8s_monitoring_api_key"],
                    },
                },
                {
                    "name": "grafana-cloud-logs",
                    "type": "loki",
                    "url": "https://logs-prod-us-central1.grafana.net./loki/api/v1/push",
                    "auth": {
                        "type": "basic",
                        "username": grafana_vault_secrets[
                            "k8s_monitoring_logs_username"
                        ],
                        "password": grafana_vault_secrets["k8s_monitoring_api_key"],
                    },
                },
                {
                    "name": "gc-otlp-endpoint",
                    "type": "otlp",
                    "url": "https://otlp-gateway-prod-us-central-0.grafana.net./otlp",
                    "protocol": "http",
                    "auth": {
                        "type": "basic",
                        "username": grafana_vault_secrets[
                            "k8s_monitoring_tracing_username"
                        ],
                        "password": grafana_vault_secrets["k8s_monitoring_api_key"],
                    },
                    "metrics": {
                        "enabled": True,
                    },
                    "logs": {
                        "enabled": True,
                    },
                    "traces": {
                        "enabled": True,
                    },
                },
            ],
            "clusterMetrics": {
                "enabled": True,
                "opencost": {
                    "enabled": True,
                    "metricsSource": "grafana-cloud-metrics",
                    "opencost": {
                        "exporter": {
                            "defaultClusterId": cluster_name,
                        },
                        "prometheus": {
                            "existingSecretName": "grafana-cloud-metrics-grafana-k8s-monitoring",  # pragma: allowlist secret
                            "external": {
                                "url": "https://prometheus-prod-10-prod-us-central-0.grafana.net./api/prom"
                            },
                        },
                    },
                },
                "kube-state-metrics": {"deploy": True},
                "kepler": {
                    "enabled": True,
                },
            },
            "annotationAutodiscover": {
                "enabled": True,
            },
            "prometheusOperatorObjects": {
                "enabled": True,
            },
            "clusterEvents": {
                "enabled": True,
            },
            "podLogs": {
                "enabled": True,
            },
            "applicationObservability": {
                "enabled": True,
                "receivers": {
                    "otlp": {
                        "grpc": {
                            "enabled": True,
                            "port": 4317,
                        },
                        "http": {
                            "enabled": True,
                            "port": 4318,
                        },
                    },
                    "zipkin": {
                        "enabled": True,
                        "port": 9411,
                    },
                },
            },
            "alloy-metrics": {
                "enabled": True,
                "alloy": {
                    "extraEnv": alloy_extra_env_vars,
                },
                "remoteConfig": {
                    "enabled": True,
                    "url": "https://fleet-management-prod-001.grafana.net",
                    "auth": {
                        "type": "basic",
                        "username": grafana_vault_secrets[
                            "k8s_monitoring_tracing_username"
                        ],
                        "password": grafana_vault_secrets["k8s_monitoring_api_key"],
                    },
                },
            },
            "alloy-singleton": {
                "enabled": True,
                "alloy": {
                    "extraEnv": alloy_extra_env_vars,
                },
                "remoteConfig": {
                    "enabled": True,
                    "url": "https://fleet-management-prod-001.grafana.net",
                    "auth": {
                        "type": "basic",
                        "username": grafana_vault_secrets[
                            "k8s_monitoring_tracing_username"
                        ],
                        "password": grafana_vault_secrets["k8s_monitoring_api_key"],
                    },
                },
            },
            "alloy-logs": {
                "enabled": True,
                "alloy": {
                    "extraEnv": alloy_extra_env_vars,
                },
                "remoteConfig": {
                    "enabled": True,
                    "url": "https://fleet-management-prod-001.grafana.net",
                    "auth": {
                        "type": "basic",
                        "username": grafana_vault_secrets[
                            "k8s_monitoring_tracing_username"
                        ],
                        "password": grafana_vault_secrets["k8s_monitoring_api_key"],
                    },
                },
            },
            "alloy-receiver": {
                "enabled": True,
                "alloy": {
                    "extraEnv": alloy_extra_env_vars,
                    "extraPorts": [
                        {
                            "name": "otlp-grpc",
                            "port": 4317,
                            "targetPort": 4317,
                            "protocol": "TCP",
                        },
                        {
                            "name": "otlp-http",
                            "port": 4318,
                            "targetPort": 4318,
                            "protocol": "TCP",
                        },
                        {
                            "name": "zipkin",
                            "port": 9411,
                            "targetPort": 9411,
                            "protocol": "TCP",
                        },
                    ],
                },
                "remoteConfig": {
                    "enabled": True,
                    "url": "https://fleet-management-prod-001.grafana.net",
                    "auth": {
                        "type": "basic",
                        "username": grafana_vault_secrets[
                            "k8s_monitoring_tracing_username"
                        ],
                        "password": grafana_vault_secrets["k8s_monitoring_api_key"],
                    },
                },
            },
            "integrations": {
                "dcgm-exporter": {
                    "instances": [
                        {
                            "name": "dcgm-exporter",
                            "labelSelectors": {
                                "app.kubernetes.io/name": "dcgm-exporter",
                            },
                        }
                    ],
                },
            },
        },
    ),
    opts=ResourceOptions(provider=k8s_provider, delete_before_replace=True),
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

node_feature_discovery_crds = kubernetes.yaml.v2.ConfigGroup(
    f"{cluster_name}-nfd-crds",
    files=[
        "https://raw.githubusercontent.com/kubernetes-sigs/node-feature-discovery/master/deployment/base/nfd-crds/nfd-api-crds.yaml",
    ],
    opts=ResourceOptions(
        provider=k8s_provider,
        delete_before_replace=True,
    ),
)
nvidia_k8s_device_plugin_release = kubernetes.helm.v3.Release(
    f"{cluster_name}-nvidia-k8s-device-plugin-helm-release",
    kubernetes.helm.v3.ReleaseArgs(
        name="nvidia-device-plugin",
        chart="nvidia-device-plugin",
        version=VERSIONS["NVIDIA_K8S_DEVICE_PLUGIN_VERSION"],
        namespace="operations",
        repository_opts=kubernetes.helm.v3.RepositoryOptsArgs(
            repo="https://nvidia.github.io/k8s-device-plugin"
        ),
        cleanup_on_fail=True,
        skip_await=True,
        values={
            "affinity": {
                "nodeAffinity": {
                    "requiredDuringSchedulingIgnoredDuringExecution": {
                        "nodeSelectorTerms": [
                            {
                                "matchExpressions": [
                                    {
                                        "key": "ol.mit.edu/gpu_node",
                                        "operator": "In",
                                        "values": ["true"],
                                    }
                                ]
                            }
                        ]
                    }
                }
            },
            "tolerations": [
                {
                    "key": "ol.mit.edu/gpu_node",
                    "operator": "Equal",
                    "value": "true",
                    "effect": "NoSchedule",
                }
            ],
            "gfd": {
                "enabled": True,
            },
            "nfd": {
                "master": {
                    "resources": {
                        "requests": {
                            "cpu": "10m",
                            "memory": "100Mi",
                        },
                        "limits": {
                            "memory": "100Mi",
                        },
                    },
                },
                "worker": {
                    "resources": {
                        "requests": {
                            "cpu": "5m",
                            "memory": "20Mi",
                        },
                        "limits": {
                            "memory": "20Mi",
                        },
                    },
                    "tolerations": [
                        {
                            "key": "ol.mit.edu/gpu_node",
                            "operator": "Equal",
                            "value": "true",
                            "effect": "NoSchedule",
                        },
                    ],
                },
            },
            "config": {
                "map": {
                    "default": "version: v1\nsharing:\n  mps:\n    resources:\n    - name: nvidia.com/gpu\n      replicas: 10\n    failRequestsGreaterThanOne: false\n",
                }
            },
            "resources": {
                "requests": {
                    "cpu": "10m",
                    "memory": "100Mi",
                },
                "limits": {
                    "memory": "100Mi",
                },
            },
        },
    ),
    opts=ResourceOptions(
        provider=k8s_provider,
        parent=k8s_provider,
        delete_before_replace=True,
    ),
)

nvidia_dcgm_exporter_release = kubernetes.helm.v3.Release(
    f"{cluster_name}-nvidia-dcgm-exporter-helm-release",
    kubernetes.helm.v3.ReleaseArgs(
        name="nvidia-dcgm-exporter",
        chart="dcgm-exporter",
        version=VERSIONS["NVIDIA_DCGM_EXPORTER_VERSION"],
        namespace="operations",
        repository_opts=kubernetes.helm.v3.RepositoryOptsArgs(
            repo="https://nvidia.github.io/dcgm-exporter/helm-charts"
        ),
        cleanup_on_fail=True,
        skip_await=True,
        values={
            "tolerations": [
                {
                    "key": "ol.mit.edu/gpu_node",
                    "operator": "Equal",
                    "value": "true",
                    "effect": "NoSchedule",
                }
            ],
            "resources": {
                "requests": {
                    "cpu": "10m",
                    "memory": "512Mi",
                },
                "limits": {
                    "memory": "512Mi",
                },
            },
            "nodeSelector": {
                "ol.mit.edu/gpu_node": "true",
            },
            "serviceMonitor": {
                "enabled": False,
            },
        },
    ),
    opts=ResourceOptions(
        provider=k8s_provider,
        parent=k8s_provider,
        depends_on=[node_feature_discovery_crds],
        delete_before_replace=True,
    ),
)
