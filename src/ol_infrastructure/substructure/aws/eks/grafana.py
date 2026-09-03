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


def _keycloak_olapps_idp_login_redact_alloy_config() -> str:
    """
    Alloy River stages that redact PII from olapps-realm brokered-login
    events and let the rest of the line through to Loki, so
    dashboards/keycloak_olapps_idp_logins.py can count logins per IdP
    directly from LogQL (count_over_time) instead of from a derived
    Prometheus counter.

    An earlier version of this pipeline extracted a Prometheus counter via
    stage.metrics instead of keeping any log record. That turned out to have
    three compounding problems, all found via live QA testing: (1)
    metric.counter's `source` is required, not optional, to actually
    increment (unset, it looks for a field that never exists and silently
    never fires); (2) `source` alone doesn't attach a label, so a separate
    stage.labels promotion was needed for a per-IdP breakdown; (3)
    max_idle_duration drops and recreates the counter after any gap between
    logins, and because Prometheus's increase() only detects a reset via a
    *decrease* (not a gap), a reset landing on the same value as before it
    silently reports zero increase -- and metric.counter's own first-ever
    appearance (no prior sample to diff against) hits the identical blind
    spot on every single login until a second one confirms a real delta.
    Every fix for one of these surfaced the next. Redacting and keeping the
    actual log line sidesteps all three: count_over_time reads directly from
    Loki's stored lines at query time, so there's no persistent counter
    state to evict, reset, or have a cold start.

    Keycloak's jboss-logging event listener is configured (see
    applications/keycloak/__main__.py) with success-level=info so that
    successful LOGIN/IDENTITY_PROVIDER_LOGIN events reach our logs; that
    setting is all-or-nothing per Keycloak, logging every successful event
    type (CODE_TO_TOKEN, REFRESH_TOKEN, LOGOUT, REGISTER, ...) across every
    realm on the shared instance, not just logins on olapps.

    1. Match INFO-level LOGIN/IDENTITY_PROVIDER_LOGIN events on olapps that
       carry an identity_provider detail (i.e. were actually brokered
       through an external IdP, not a direct username/password login), and
       redact username and identity_provider_identity (the federated email)
       -- the only PII these events carry -- in place. identity_provider
       itself is just an IdP alias (e.g. "touchstone-idp"), not PII, and is
       left untouched so the dashboard can query and group by it. The
       (redacted) line is kept, not dropped.
    2. Drop every other INFO-level org.keycloak.events line: both direct
       (non-brokered) olapps logins, which this dashboard doesn't cover, and
       the rest of the noisy INFO volume (CODE_TO_TOKEN, REFRESH_TOKEN, etc.)
       that success-level=info produces across every realm. The selector
       excludes lines carrying identity_provider=", since those are exactly
       the ones stage 1 already redacted and wants kept.
    3. Match WARN-level LOGIN_ERROR/IDENTITY_PROVIDER_LOGIN_ERROR events on
       olapps that carry an identity_provider detail, and redact username the
       same way. These were never dropped -- WARN/ERROR events are the
       pre-existing failure logs already relied on for debugging (e.g.
       diagnosing a broken IdP integration) -- so this only adds the same
       redaction, not a change in what reaches Loki.

    Each stage's selector independently re-checks the INFO/WARN level
    marker and the olapps realm, rather than relying on another stage
    having already scoped the pipeline, so this is correct regardless of
    stage ordering.
    """
    return r"""
stage.match {
  selector = "{namespace=\"keycloak\", container=\"keycloak\"} |= \"INFO  [org.keycloak.events]\" |= \"realmId=\\\"olapps\\\"\" |~ \"type=\\\"(LOGIN|IDENTITY_PROVIDER_LOGIN)\\\"\" |= \"identity_provider=\\\"\""
  pipeline_name = "keycloak_olapps_login_success_redact"

  // expression's capture group is what gets replaced -- the surrounding
  // username="..." / identity_provider_identity="..." text outside the
  // parens is left as-is and doesn't need to be repeated in `replace`.
  stage.replace {
    expression = `username="([^"]+)"`
    replace    = `[REDACTED]`
  }

  stage.replace {
    expression = `identity_provider_identity="([^"]+)"`
    replace    = `[REDACTED]`
  }
}

stage.match {
  selector = "{namespace=\"keycloak\", container=\"keycloak\"} |= \"INFO  [org.keycloak.events]\" != \"identity_provider=\\\"\""
  action = "drop"
  drop_counter_reason = "keycloak_info_event"
}

stage.match {
  selector = "{namespace=\"keycloak\", container=\"keycloak\"} |= \"WARN  [org.keycloak.events]\" |= \"realmId=\\\"olapps\\\"\" |~ \"type=\\\"(LOGIN_ERROR|IDENTITY_PROVIDER_LOGIN_ERROR)\\\"\" |= \"identity_provider=\\\"\""
  pipeline_name = "keycloak_olapps_login_failure_redact"

  // expression's capture group is what gets replaced -- the surrounding
  // username="..." / identity_provider_identity="..." text outside the
  // parens is left as-is and doesn't need to be repeated in `replace`.
  stage.replace {
    expression = `username="([^"]+)"`
    replace    = `[REDACTED]`
  }

  stage.replace {
    expression = `identity_provider_identity="([^"]+)"`
    replace    = `[REDACTED]`
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
                                        # Keep every trace from every service
                                        # EXCEPT the few whose volume would
                                        # dominate the bill.  Policies are OR'd,
                                        # so this runs alongside the
                                        # probabilistic policy below rather than
                                        # replacing it: the named services still
                                        # get their 15% sample, everything else
                                        # is kept whole.
                                        #
                                        # Written as an inverted match rather
                                        # than a keep-list of small services on
                                        # purpose.  The failure this fixes is a
                                        # low-traffic service sampled into
                                        # invisibility -- ol-analytics-api sees
                                        # ~10 dashboard requests/day, which at
                                        # 15% is under one trace/day -- and a
                                        # keep-list only ever fixes it for
                                        # services someone remembered to add.
                                        # Inverted, a NEW service is complete by
                                        # default and has to earn its way onto
                                        # this list by getting large.
                                        #
                                        # Chosen from measured spans/6h on
                                        # 2026-09-03; these three are ~82% of
                                        # all span volume and sit an order of
                                        # magnitude above the rest:
                                        #   ...edxapp-lms-celery  13.8M
                                        #   ...edxapp-lms         10.3M
                                        #   apisix                 2.9M
                                        # Next is learn-webapp at 1.4M, left OFF
                                        # deliberately -- it is a first-party
                                        # app whose traces we want complete.
                                        # Revisit against the same query if the
                                        # distribution shifts.
                                        "name": "keep-all-but-the-highest-volume-services",
                                        "type": "string_attribute",
                                        "key": "service.name",
                                        "values": [
                                            "mitxonline-production-edxapp-lms-celery",
                                            "mitxonline-production-edxapp-lms",
                                            "apisix",
                                        ],
                                        "invert_match": True,
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
                            # Traefik's tracing.otlp exporter (see traefik.py)
                            # runs with traceVerbosity: detailed on the
                            # websecure entrypoint, which spans every internal
                            # middleware Traefik invokes per request --
                            # including the "Metrics" middleware, which does
                            # no request-handling work of its own -- it only
                            # records Prometheus counters/histograms. That
                            # made "Metrics" Traefik's single highest-volume
                            # span, ahead of real GET/POST traffic, with no
                            # server.address or other diagnostic value
                            # (confirmed via Tempo:
                            # entry_point is exclusively "websecure" on these
                            # spans -- they are not the separate :9100
                            # Prometheus-scrape entrypoint, which carries zero
                            # trace volume).
                            #
                            # Traefik has no per-middleware trace toggle --
                            # traceVerbosity is all-or-nothing for internal
                            # middlewares -- so the span has to be dropped
                            # downstream instead. Traefik's own Prometheus
                            # metrics (what this middleware actually produces)
                            # are unaffected; only the trace span is dropped.
                            "filters": {
                                "enabled": True,
                                "traces": {
                                    "span": [
                                        'resource.attributes["service.name"] == "traefik" and name == "Metrics"',
                                    ],
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
                    + _keycloak_olapps_idp_login_redact_alloy_config(),
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
                    "kube-state-metrics": {
                        "deploy": True,
                        # kube-state-metrics only emits kube_<resource>_labels for
                        # resources named here, so without this every kube_job_*
                        # series is anonymous -- on data-production that is ~2000
                        # series where run-worker successes and failures can be
                        # counted but not attributed to anything.
                        #
                        # Dagster's K8sRunLauncher already puts dagster/job,
                        # dagster/code-location and dagster/run-id on every run
                        # Job, so allowlisting the first two turns kube_job_status_*
                        # into a per-code-location, per-job breakdown for free.
                        # dagster/run-id is deliberately excluded: it is unique per
                        # run at ~53k runs/day, which is a cardinality bomb.
                        #
                        # This is a list, so Helm replaces rather than merges it --
                        # the nodes entry is the chart's own default, repeated here
                        # verbatim so adding jobs does not silently drop the node
                        # labels that the Grafana Cloud Kubernetes app relies on.
                        # Harmless on clusters with no Dagster: they just get a
                        # kube_job_labels series per Job with no dagster_* labels.
                        "metricLabelsAllowlist": [
                            "nodes=[agentpool,alpha.eksctl.io/cluster-name,"
                            "alpha.eksctl.io/nodegroup-name,"
                            "beta.kubernetes.io/instance-type,"
                            "cloud.google.com/gke-nodepool,cluster-name,"
                            "ec2.amazonaws.com/Name,"
                            "ec2.amazonaws.com/aws-autoscaling-groupName,"
                            "ec2.amazonaws.com/aws-autoscaling-group-name,"
                            "ec2.amazonaws.com/name,eks.amazonaws.com/nodegroup,"
                            "k8s.io/cloud-provider-aws,karpenter.sh/nodepool,"
                            "kubernetes.azure.com/cluster,kubernetes.io/arch,"
                            "kubernetes.io/hostname,kubernetes.io/os,"
                            "node.kubernetes.io/instance-type,"
                            "topology.kubernetes.io/region,"
                            "topology.kubernetes.io/zone]",
                            "jobs=[dagster/code-location,dagster/job]",
                        ],
                    },
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
