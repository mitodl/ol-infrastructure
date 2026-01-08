# ruff: noqa: E501
from pathlib import Path

import pulumi_kubernetes as kubernetes
from pulumi import ResourceOptions

from bridge.lib.versions import GRAFANA_K8S_MONITORING_CHART_VERSION
from bridge.secrets.sops import read_yaml_secrets
from ol_infrastructure.lib.pulumi_helper import StackInfo


def setup_grafana(
    cluster_name: str,
    stack_info: StackInfo,
    k8s_provider: kubernetes.Provider,
    grafana_k8s_monitoring_version: str = GRAFANA_K8S_MONITORING_CHART_VERSION,
):
    """
    Set up Grafana k8s-monitoring resources including Helm chart installation.

    Skips installation for CI tier clusters.

    Args:
        cluster_name: The name of the EKS cluster.
        stack_info: The StackInfo object containing environment information.
        k8s_provider: The Pulumi Kubernetes provider instance.
        grafana_k8s_monitoring_version: The version of the Grafana k8s-monitoring chart.
    """
    if stack_info.env_suffix.lower() == "ci":
        return

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

    kubernetes.helm.v3.Release(
        f"{cluster_name}-grafana-k8s-monitoring-helm-release",
        kubernetes.helm.v3.ReleaseArgs(
            name="grafana-k8s-monitoring",
            chart="k8s-monitoring",
            version=grafana_k8s_monitoring_version,
            namespace="grafana",
            create_namespace=True,
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
                        "processors": {
                            "tailSampling": {
                                "enabled": True,
                                "decisionWait": "5s",
                                "numTraces": 100,
                                "expectedNewTracesPerSecond": 10,
                                "decisionCache": {
                                    "sampledCacheSize": 1000,
                                    "nonSampledCacheSize": 10000,
                                },
                                "policies": [
                                    {
                                        "name": "keep-errors",
                                        "type": "status_code",
                                        "status_codes": ["ERROR", "UNSET"],
                                    },
                                    {
                                        "name": "sample-slow-traces",
                                        "type": "latency",
                                        "threshold_ms": 5000,
                                    },
                                    {
                                        "name": "sample-15pct-traces",
                                        "type": "probabilistic",
                                        "sampling_percentage": 15,
                                    },
                                ],
                            },
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
