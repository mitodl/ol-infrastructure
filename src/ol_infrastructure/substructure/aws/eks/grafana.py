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


def _keycloak_olapps_idp_login_metrics_alloy_config() -> str:
    """
    Alloy River stages that turn olapps-realm brokered-login events into
    per-IdP Prometheus counters, without shipping login records (or the PII
    they carry) to Loki.

    Keycloak's jboss-logging event listener is configured (see
    applications/keycloak/__main__.py) with success-level=info so that
    successful LOGIN/IDENTITY_PROVIDER_LOGIN events reach our logs; that
    setting is all-or-nothing per Keycloak, logging every successful event
    type (CODE_TO_TOKEN, REFRESH_TOKEN, LOGOUT, REGISTER, ...) across every
    realm on the shared instance, not just logins on olapps. Those events
    also carry username and identity_provider_identity (the federated
    email) -- fields the dashboard doesn't need, since it only wants a
    per-IdP count.

    So rather than filter down to the wanted events and ship them to Loki,
    this pipeline extracts what's needed as metrics and ships no
    olapps-brokered-login records to Loki at all:

    1. Match INFO-level LOGIN/IDENTITY_PROVIDER_LOGIN events on olapps that
       carry an identity_provider detail (i.e. were actually brokered
       through an external IdP, not a direct username/password login), and
       increment keycloak_olapps_idp_login_total{identity_provider=...}.
    2. Unconditionally drop every remaining INFO-level org.keycloak.events
       line -- covering both the events just counted (no login record, only
       the counter, reaches Loki) and the rest of the noisy INFO volume
       (CODE_TO_TOKEN, REFRESH_TOKEN, etc.) that success-level=info produces.
    3. Match WARN-level LOGIN_ERROR/IDENTITY_PROVIDER_LOGIN_ERROR events on
       olapps that carry an identity_provider detail, and increment
       keycloak_olapps_idp_login_failure_total{identity_provider=...}. This
       stage has no drop action: WARN/ERROR events are the pre-existing
       failure logs already relied on for debugging (e.g. diagnosing a
       broken IdP integration) and carry materially less PII than the
       success events, so they keep flowing to Loki unchanged -- this stage
       only adds a metric as a side effect.

    Each stage's selector independently re-checks the INFO/WARN level
    marker and the olapps realm, rather than relying on another stage
    having already scoped the pipeline, so this is correct regardless of
    stage ordering.
    """
    return r"""
stage.match {
  selector = "{namespace=\"keycloak\", container=\"keycloak\"} |= \"INFO  [org.keycloak.events]\" |= \"realmId=\\\"olapps\\\"\" |~ \"type=\\\"(LOGIN|IDENTITY_PROVIDER_LOGIN)\\\"\" |= \"identity_provider=\\\"\""
  pipeline_name = "keycloak_olapps_login_success_metric"

  stage.regex {
    expression = `identity_provider="(?P<identity_provider>[^"]+)"`
  }

  // metric.counter needs BOTH of these, for two unrelated reasons:
  //   - source: when unset, it defaults to the metric's own `name`, which
  //     is never a key in the extracted map -- so the counter would look
  //     for a field that doesn't exist and silently never increment.
  //     Setting it to "identity_provider" makes it require that (real)
  //     extracted field's presence instead.
  //   - stage.labels: source only *gates* on the extracted map, it doesn't
  //     attach the extracted field as a label on the resulting metric.
  //     Without this, every login (any IdP) increments one undifferentiated
  //     series. This promotes identity_provider to an actual label on the
  //     log entry first, so stage.metrics carries it as a metric label too.
  stage.labels {
    values = {
      identity_provider = "",
    }
  }

  stage.metrics {
    metric.counter {
      name        = "keycloak_olapps_idp_login_total"
      description = "Successful olapps realm logins brokered through an external identity provider, by IdP alias"
      source      = "identity_provider"
      action      = "inc"
      // max_idle_duration defaults to "5m": a counter with no matching line
      // for 5 minutes is dropped, and the next matching login starts a
      // fresh series back at 1. When a reset lands on the same value as
      // before the gap (e.g. 1 -> idle-dropped -> next login recreates it
      // at 1), Prometheus's increase() can't tell a reset happened -- it
      // only detects resets via a *decrease* between consecutive samples,
      // not a gap -- so it silently reports zero increase across that
      // boundary, for any window width, confirmed directly against two
      // real test logins ~7m apart.
      //
      // There's no duration that makes this fully watertight -- any IdP
      // whose login gap exceeds max_idle_duration hits the same silent
      // undercount, just at a longer interval. 2160h (90 days -- Alloy's
      // River duration parser has no "d" unit, only up to "h"; a literal
      // "90d" fails to parse and silently breaks the whole containing
      // loki.process component on every reload, confirmed the hard way)
      // is chosen to comfortably outlast the widest range anyone would
      // realistically view this dashboard at (default is 7d; even a
      // manually-widened 30-60d query stays inside a single continuous
      // series). It only misses a login if a given IdP goes completely
      // unused for >90d *and* someone then queries a range spanning that
      // exact gap -- an accepted, bounded tradeoff rather than a full fix.
      max_idle_duration = "2160h"
      // metric.counter defaults prefix to "loki_process_custom_", and an
      // explicit empty string does not override that (Alloy appears to
      // treat "" the same as unset). So the real exposed/exported name is
      // loki_process_custom_keycloak_olapps_idp_login_total -- confirmed
      // directly on Alloy's own /metrics endpoint -- not the bare name
      // this block sets via `name`. Every consumer (the includeMetrics
      // allowlist entry below, and every PromQL query in
      // dashboards/keycloak_olapps_idp_logins.py) must use the full
      // loki_process_custom_-prefixed name, not this bare one.
    }
  }
}

stage.match {
  selector = "{namespace=\"keycloak\", container=\"keycloak\"} |= \"INFO  [org.keycloak.events]\""
  action = "drop"
  drop_counter_reason = "keycloak_info_event"
}

stage.match {
  selector = "{namespace=\"keycloak\", container=\"keycloak\"} |= \"WARN  [org.keycloak.events]\" |= \"realmId=\\\"olapps\\\"\" |~ \"type=\\\"(LOGIN_ERROR|IDENTITY_PROVIDER_LOGIN_ERROR)\\\"\" |= \"identity_provider=\\\"\""
  pipeline_name = "keycloak_olapps_login_failure_metric"

  stage.regex {
    expression = `identity_provider="(?P<identity_provider>[^"]+)"`
  }

  stage.labels {
    values = {
      identity_provider = "",
    }
  }

  stage.metrics {
    metric.counter {
      name        = "keycloak_olapps_idp_login_failure_total"
      description = "Failed olapps realm logins brokered through an external identity provider, by IdP alias"
      source      = "identity_provider"
      action      = "inc"
      // See the max_idle_duration comment on the success counter above --
      // same reasoning applies here.
      max_idle_duration = "2160h"
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
                                # The sampler holds every in-flight trace in memory
                                # until decisionWait elapses, then decides.  These
                                # numbers must be sized against real trace volume:
                                # APISIX alone fronts ~280 req/s, and each of those
                                # is a distinct trace.  The previous values (100 /
                                # 10) buffered ~0.4s of traffic, so traces were
                                # evicted before their decision was ever made and
                                # ~98% of them -- including nearly every APISIX root
                                # span -- never reached Tempo.
                                #
                                # decisionWait also has to outlast the slowest
                                # exporter feeding a trace.  APISIX batches spans on
                                # a 2s timeout and the Django BatchSpanProcessor on
                                # 5s, so a 5s window routinely closed before the
                                # gateway's root span arrived, orphaning the trace.
                                "decisionWait": "15s",
                                "numTraces": 50000,
                                "expectedNewTracesPerSec": 1000,
                                # Keep decisions for trace IDs far longer than the
                                # span data itself, so spans that arrive after a
                                # trace has been released from memory inherit the
                                # original decision instead of being re-judged.
                                #
                                # Do NOT raise either of these to 1_000_000 or
                                # above: the chart renders them through Go's %v on a
                                # float64, which switches to scientific notation at
                                # 1e6 and emits `non_sampled_cache_size = 1e+06`
                                # into the Alloy config.  Verified against chart
                                # 4.3.2 with `helm template`; 999_999 renders as an
                                # integer, 1_000_000 does not.
                                "decisionCache": {
                                    "sampledCacheSize": 500000,
                                    "nonSampledCacheSize": 900000,
                                },
                                "policies": [
                                    {
                                        # UNSET is the status of virtually every
                                        # successful span, so including it here made
                                        # this policy match all traffic and silently
                                        # neutered the probabilistic policy below.
                                        "name": "keep-errors",
                                        "type": "status_code",
                                        "status_codes": ["ERROR"],
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
                                    # The chart ships the sampler with no requests
                                    # or limits, which left it BestEffort -- first
                                    # in line for eviction under node pressure.
                                    # That is untenable now that numTraces buffers
                                    # ~50k traces (order 1GiB) instead of 100.  No
                                    # CPU limit: throttling the sampler stalls the
                                    # trace pipeline for the whole cluster.
                                    "alloy": {
                                        "resources": {
                                            "requests": {
                                                "cpu": "500m",
                                                "memory": "1Gi",
                                            },
                                            "limits": {"memory": "2Gi"},
                                        },
                                    },
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
                    "extraLogProcessingStages": _apisix_cookie_metrics_alloy_config()
                    + _keycloak_olapps_idp_login_metrics_alloy_config(),
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
                    # Without this, collectors.getCollectorForFeature falls
                    # back to "use the only enabled collector" -- which is
                    # ambiguous here (4 named collectors), so it resolves to
                    # an empty collector name and the *entire* integrations
                    # feature (this alloy integration AND the pre-existing
                    # dcgm-exporter one) is silently dropped from every
                    # collector's rendered config. Confirmed live: neither
                    # integration appeared in any of the 4 collector
                    # ConfigMaps in applications-production despite the Helm
                    # release values matching desired state.
                    "collector": "alloy-metrics",
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
                    # Chart-native self-monitoring for the Alloy collectors
                    # themselves (feature-integrations' integrations.alloy).
                    # Discovers every collector Deployment/StatefulSet the
                    # chart creates (alloy-logs, alloy-metrics,
                    # alloy-receiver, alloy-singleton, and the tail-sampling
                    # collector, which the alloy-operator names plain
                    # "alloy") on their shared http-metrics/12345 port --
                    # verified against the live applications-production
                    # Service objects and this chart version's default
                    # port_name (also http-metrics).
                    #
                    # useDefaultAllowList (on by default) keeps this to the
                    # chart's curated ~90-series set instead of scraping
                    # every alloy_component_* and go runtime metric
                    # unfiltered. includeMetrics extends that allowlist with
                    # the tail-sampling processor's own counters, which
                    # aren't in the default set: these are what would have
                    # surfaced the tail-sampler sizing bug (traces evicted
                    # before a decision was made, buffer occupancy far past
                    # the old 100-trace cap) instead of it going unnoticed.
                    #
                    # Also extended with the two keycloak_olapps_idp_login_*
                    # counters (see dashboards/keycloak_olapps_idp_logins.py
                    # and substructure/aws/eks/grafana.py's
                    # _keycloak_olapps_idp_login_metrics_alloy_config) --
                    # custom stage.metrics counters are exactly the
                    # "unfiltered alloy_component_*" case this allowlist
                    # exists to block by default, so without an explicit
                    # entry here they're silently dropped before ever
                    # reaching Mimir: confirmed present on Alloy's own
                    # /metrics endpoint but absent from Mimir at every range
                    # queried, however wide.
                    #
                    # Note the loki_process_custom_ prefix: metric.counter
                    # defaults to that prefix and an explicit `prefix = ""`
                    # does not override it (confirmed directly against
                    # Alloy's /metrics endpoint), so the real exported names
                    # are loki_process_custom_keycloak_olapps_idp_login_total
                    # and its _failure_total counterpart, not the bare names
                    # set via metric.counter's `name` attribute.
                    "alloy": {
                        "instances": [
                            {
                                "name": "alloy-collectors",
                                "labelSelectors": {
                                    "app.kubernetes.io/name": [
                                        "alloy",
                                        "alloy-logs",
                                        "alloy-metrics",
                                        "alloy-receiver",
                                        "alloy-singleton",
                                    ],
                                },
                                "namespaces": ["grafana"],
                                "metrics": {
                                    "tuning": {
                                        "includeMetrics": [
                                            "otelcol_processor_tail_sampling_count_traces_sampled",
                                            "otelcol_processor_tail_sampling_sampling_trace_dropped_too_early",
                                            "otelcol_processor_tail_sampling_new_trace_id_received",
                                            "otelcol_processor_tail_sampling_sampling_traces_on_memory",
                                            "otelcol_processor_tail_sampling_sampling_policy_evaluation_error",
                                            "loki_process_custom_keycloak_olapps_idp_login_total",
                                            "loki_process_custom_keycloak_olapps_idp_login_failure_total",
                                        ],
                                    },
                                },
                            },
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
