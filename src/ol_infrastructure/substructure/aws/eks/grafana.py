# ruff: noqa: E501
from pathlib import Path

import pulumi_kubernetes as kubernetes
from pulumi import ResourceOptions

from bridge.lib.versions import GRAFANA_K8S_MONITORING_CHART_VERSION
from bridge.secrets.sops import read_yaml_secrets
from ol_infrastructure.lib.pulumi_helper import StackInfo


def _apisix_cookie_metrics_alloy_config() -> str:
    """
    Alloy River stage blocks that extract cookie header telemetry from APISix
    access logs and emit Prometheus histograms.

    This string is injected into the loki.process "pod_logs" component by the
    k8s-monitoring chart via podLogsViaLoki.extraLogProcessingStages. It runs
    after the standard relabeling stages so retained stream labels (e.g.
    service, namespace, and container) are already available.

    The APISix access log format appends cookie telemetry at the end of each
    line. This pipeline extracts the three numeric fields:
      cookie_bytes=NNN cookie_count=N oidc_session_bytes=NNN

    The stage.metrics blocks emit Prometheus histograms that Alloy exposes on
    its own metrics endpoint (:12345/metrics), from where they are scraped and
    forwarded to Grafana Cloud Prometheus.

    Privacy: only numeric cookie metrics are extracted; cookie values are never
    logged or extracted.
    """
    return r"""
stage.match {
  selector = "{service=\"apisix\"} |= \"cookie_bytes=\""
  pipeline_name = "apisix_cookie_metrics"

  // Extract host, status, and the three numeric cookie fields appended at the
  // end of each APISix access log line. host and status appear early in the
  // line; the cookie fields are always at the tail after request="...".
  stage.regex {
    expression = `.*\bhost=(?P<host>\S+).*\bstatus=(?P<status>\d+).*\bcookie_bytes=(?P<cookie_bytes>\d+)\s+cookie_count=(?P<cookie_count>\d+)\s+oidc_session_bytes=(?P<oidc_session_bytes>\d+)`
  }

  stage.metrics {
    metric.histogram {
      name        = "apisix_cookie_header_bytes"
      description = "Size in bytes of the Cookie request header at the APISix ingress, per virtual host"
      source      = "cookie_bytes"
      buckets     = [256, 512, 1024, 2048, 4096, 8192, 16384, 32768, 65536]
    }

    metric.histogram {
      name        = "apisix_cookie_count"
      description = "Number of cookies in the Cookie request header at APISix ingress"
      source      = "cookie_count"
      buckets     = [1, 2, 3, 5, 8, 12, 20, 30, 50]
    }

    metric.histogram {
      name        = "apisix_oidc_session_cookie_bytes"
      description = "Size in bytes of the APISix OIDC session cookie only"
      source      = "oidc_session_bytes"
      buckets     = [0, 256, 1024, 2048, 4096, 6144, 8192]
    }
  }
}
"""


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
                # v4: destinations is now a map (keyed by name) instead of an array
                "destinations": {
                    "grafana-cloud-metrics": {
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
                    "grafana-cloud-logs": {
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
                    "gc-otlp-endpoint": {
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
                                "expectedNewTracesPerSec": 10,
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
                                # chart v4.0.0 bug: alloy-sampler.yaml passes an
                                # incomplete context (missing .Chart/.Release) when
                                # calling collectors.remoteConfig.alloy. If the
                                # sampler inherits collectorCommon.alloy.remoteConfig
                                # (enabled + inline credentials), the template crashes
                                # on secrets.kubernetesSecretName. Disable remoteConfig
                                # explicitly on the sampler collector to avoid this.
                                "collector": {
                                    "remoteConfig": {"enabled": False},
                                },
                            },
                        },
                    },
                },
                "clusterMetrics": {
                    "enabled": True,
                    "collector": "alloy-metrics",
                },
                # v4: opencost moved from clusterMetrics to costMetrics feature
                "costMetrics": {
                    "enabled": True,
                    "collector": "alloy-metrics",
                },
                "annotationAutodiscovery": {
                    "enabled": True,
                    "collector": "alloy-metrics",
                },
                "prometheusOperatorObjects": {
                    "enabled": True,
                    "collector": "alloy-metrics",
                },
                "clusterEvents": {
                    "enabled": True,
                    "collector": "alloy-singleton",
                },
                # v4: podLogs renamed to podLogsViaLoki
                "podLogsViaLoki": {
                    "enabled": True,
                    "collector": "alloy-logs",
                    "extraLogProcessingStages": _apisix_cookie_metrics_alloy_config(),
                },
                "applicationObservability": {
                    "enabled": True,
                    "collector": "alloy-receiver",
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
                # v4: kepler and kube-state-metrics moved to telemetryServices;
                #     opencost moved here from clusterMetrics
                "telemetryServices": {
                    "kube-state-metrics": {"deploy": True},
                    "kepler": {"deploy": True},
                    "opencost": {
                        "deploy": True,
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
                },
                # v4: remoteConfig and shared extraEnv go under collectorCommon.alloy;
                #     named alloy instances replaced by collectors map with presets
                "collectorCommon": {
                    "alloy": {
                        "extraEnv": alloy_extra_env_vars,
                        "remoteConfig": {
                            "enabled": True,
                            "url": "https://fleet-management-prod-001.grafana.net",
                            "auth": {
                                "type": "basic",
                                "username": grafana_vault_secrets[
                                    "k8s_monitoring_tracing_username"
                                ],
                                "password": grafana_vault_secrets[
                                    "k8s_monitoring_api_key"
                                ],
                            },
                        },
                    },
                },
                "collectors": {
                    "alloy-metrics": {
                        "presets": ["clustered", "statefulset"],
                    },
                    "alloy-singleton": {
                        "presets": ["singleton"],
                    },
                    "alloy-logs": {
                        "presets": ["filesystem-log-reader", "daemonset"],
                    },
                    "alloy-receiver": {
                        "presets": ["deployment"],
                        "alloy": {
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
                    },
                },
            },
        ),
        opts=ResourceOptions(provider=k8s_provider, delete_before_replace=True),
    )
