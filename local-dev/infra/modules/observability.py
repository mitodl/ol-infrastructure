"""Grafana, Loki, and Alloy for the local-dev infra stack.

Provisions a self-contained logging pipeline so every pod in the cluster --
apps, Celery workers, Postgres, Valkey, OpenSearch, Keycloak, APISIX -- is
searchable from one place instead of one `kubectl logs` invocation per
deployment:

    every pod's stdout
      -> /var/log/pods/*/*/*.log        (hostPath, read-only)
      -> Alloy DaemonSet                discovery.kubernetes -> local.file_match
                                        -> loki.source.file -> stage.cri
      -> Loki                           filesystem storage, PVC, retention
      -> Grafana                        https://grafana.<root_domain>

This mirrors the production shape (see
``src/ol_infrastructure/substructure/aws/eks/grafana.py``), where the
k8s-monitoring Helm chart runs Alloy with the ``filesystem-log-reader`` preset
and ships to Grafana Cloud.  Two deliberate divergences: Loki and Grafana are
self-hosted here rather than Grafana Cloud, and Alloy is configured by hand
rather than through the k8s-monitoring chart, which would drag in four
collectors plus kube-state-metrics, opencost and kepler for no local benefit.

Alloy also exposes an OTLP receiver on 4317/4318 wired to Loki's native OTLP
endpoint.  Nothing emits to it yet -- it exists so an app can opt into
prod-style OTLP export by setting ``OTEL_*`` env vars, and so Keycloak's
``OTEL_SDK_DISABLED=true`` workaround in identity_core.py (added precisely
because no receiver existed) can be lifted.
"""

import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass

import pulumi_kubernetes as k8s
import yaml as pyyaml
from pulumi import ResourceOptions

NAMESPACE = "operations"

LOKI_IMAGE = "grafana/loki:3.3.2"
ALLOY_IMAGE = "grafana/alloy:v1.7.5"
# >= 11.6.11 is the floor Grafana documents for the Logs Drilldown app below.
GRAFANA_IMAGE = "grafana/grafana:11.6.16"

# Logs Drilldown -- the "Logs" entry under Drilldown in the nav. It browses
# services, fields and patterns without anyone writing LogQL, which is the
# whole point of having Loki here rather than `kubectl logs`.
#
# Grafana already preinstalls this by default, but asynchronously: the server
# starts listening before the download finishes, so on a fresh PVC the first
# page load builds a nav with no Logs entry, and it only appears after a
# restart. Naming it here and forcing a synchronous install is what makes it
# show up on a cold `tilt up` -- the install now completes before the HTTP
# listener opens. The list adds to Grafana's defaults rather than replacing
# them, so the Pyroscope app comes along too; harmless, if a dead "Profiles"
# nav entry until something serves profiles.
#
# Unpinned on purpose: the plugin catalog resolves the newest build whose
# grafanaDependency matches GRAFANA_IMAGE, so this cannot drift out of sync
# with a Grafana bump. A failed download is logged and start-up continues, so
# an offline laptop loses the app, not the cluster.
GRAFANA_PLUGINS = "grafana-lokiexplore-app"

LOKI_URL = f"http://loki.{NAMESPACE}.svc.cluster.local:3100"
OTLP_HTTP_ENDPOINT = f"http://alloy.{NAMESPACE}.svc.cluster.local:4318"

# Loki rejects a retention window that is not a whole multiple of the index
# period, and silently keeps the default instead of failing loudly.
INDEX_PERIOD_HOURS = 24
_RETENTION_RE = re.compile(r"^(\d+)([hd])$")

# Alloy's River config. Pod discovery is deliberately *not* filtered to the
# local node: local.file_match only matches files that exist on this node's
# filesystem, so pods scheduled elsewhere drop out on their own and we avoid
# threading a downward-API node name through the config.
#
# loki.source.kubernetes (log streaming via the API server) would need one pod
# instead of a DaemonSet, but it rides the same kubelet :10250 streaming path
# that is documented to wedge after VM sleep (local-dev/scripts/heal-exec.sh),
# which would kill log collection exactly when the environment is already
# broken. Reading files off the node has no such dependency, and matches prod.
ALLOY_CONFIG = f"""\
discovery.kubernetes "pods" {{
  role = "pod"
}}

discovery.relabel "pod_logs" {{
  targets = discovery.kubernetes.pods.targets

  rule {{
    source_labels = ["__meta_kubernetes_namespace"]
    target_label  = "namespace"
  }}
  rule {{
    source_labels = ["__meta_kubernetes_pod_name"]
    target_label  = "pod"
  }}
  rule {{
    source_labels = ["__meta_kubernetes_pod_container_name"]
    target_label  = "container"
  }}
  rule {{
    source_labels = ["__meta_kubernetes_pod_label_app"]
    target_label  = "app"
  }}

  // /var/log/pods/<namespace>_<pod>_<uid>/<container>/*.log
  rule {{
    source_labels = [
      "__meta_kubernetes_pod_uid",
      "__meta_kubernetes_pod_container_name",
    ]
    separator     = "/"
    action        = "replace"
    replacement   = "/var/log/pods/*$1/*.log"
    target_label  = "__path__"
  }}
}}

local.file_match "pod_logs" {{
  path_targets = discovery.relabel.pod_logs.output
}}

loki.source.file "pod_logs" {{
  targets    = local.file_match.pod_logs.targets
  forward_to = [loki.process.pod_logs.receiver]
}}

loki.process "pod_logs" {{
  // Strips the containerd "<ts> stdout F " prefix off every line.
  stage.cri {{}}

  forward_to = [loki.write.local.receiver]
}}

loki.write "local" {{
  endpoint {{
    url = "{LOKI_URL}/loki/api/v1/push"
  }}
}}

// OTLP receiver -- production parity. Nothing emits here yet; an app opts in
// by pointing OTEL_EXPORTER_OTLP_LOGS_ENDPOINT at {OTLP_HTTP_ENDPOINT}/v1/logs.
otelcol.receiver.otlp "default" {{
  grpc {{
    endpoint = "0.0.0.0:4317"
  }}
  http {{
    endpoint = "0.0.0.0:4318"
  }}

  output {{
    logs = [otelcol.processor.batch.default.input]
  }}
}}

otelcol.processor.batch "default" {{
  output {{
    logs = [otelcol.exporter.otlphttp.loki.input]
  }}
}}

otelcol.exporter.otlphttp "loki" {{
  client {{
    endpoint = "{LOKI_URL}/otlp"
  }}
}}
"""


def _checksum(content: str) -> str:
    """Return a stable short digest of *content* for a pod-template annotation.

    Deliberately not ``hash()``: Python randomises string hashing per process,
    so that would roll every pod on every ``pulumi up``.
    """
    return hashlib.sha256(content.encode()).hexdigest()[:16]


@dataclass
class ObservabilityResources:
    """Resources created by the observability module."""

    loki: k8s.apps.v1.Deployment
    alloy: k8s.apps.v1.DaemonSet
    grafana: k8s.apps.v1.Deployment


def validate_retention_period(value: str) -> str:
    """Return *value* if Loki will honour it as a retention period.

    Loki requires ``retention_period`` to be a whole multiple of the 24h index
    period; anything else is ignored in favour of the default rather than
    rejected, which would silently discard logs early or keep them forever.
    Fail the deploy instead -- the likeliest source of a bad value is a typo in
    a developer's gitignored tilt_config.json.

    :raises SystemExit: if *value* is not a whole number of days.
    """
    match = _RETENTION_RE.match(value.strip())
    if match:
        amount, unit = int(match.group(1)), match.group(2)
        hours = amount if unit == "h" else amount * INDEX_PERIOD_HOURS
        if hours > 0 and hours % INDEX_PERIOD_HOURS == 0:
            return f"{hours}h"

    msg = (
        f"Invalid log retention period: {value!r}\n"
        "Loki needs a whole number of days, written as hours or days "
        '(e.g. "168h", "7d", "48h").\n'
        'Set it with "log_retention_period" in your tilt_config.json, or '
        "LOCAL_DEV_LOG_RETENTION for a hand-run `pulumi up`."
    )
    raise SystemExit(msg)


def _loki_config(retention_period: str) -> str:
    """Render the single-binary Loki config with *retention_period* applied."""
    return pyyaml.safe_dump(
        {
            "auth_enabled": False,
            "server": {"http_listen_port": 3100, "log_level": "warn"},
            "common": {
                "instance_addr": "127.0.0.1",
                "path_prefix": "/loki",
                "storage": {
                    "filesystem": {
                        "chunks_directory": "/loki/chunks",
                        "rules_directory": "/loki/rules",
                    }
                },
                "replication_factor": 1,
                "ring": {"kvstore": {"store": "inmemory"}},
            },
            "schema_config": {
                "configs": [
                    {
                        # tsdb + v13 are required for structured metadata,
                        # which is how OTLP attributes survive ingestion.
                        "from": "2024-04-01",
                        "store": "tsdb",
                        "object_store": "filesystem",
                        "schema": "v13",
                        "index": {
                            "prefix": "index_",
                            "period": f"{INDEX_PERIOD_HOURS}h",
                        },
                    }
                ]
            },
            "limits_config": {
                "retention_period": retention_period,
                # allow_structured_metadata/volume_enabled/discover_log_levels
                # are the three limits Logs Drilldown requires: they back its
                # detected fields, its service list, and its level breakdown
                # respectively. The last two default on in Loki 3.x; set
                # explicitly so a future default flip is not a silent
                # regression in a UI nobody is testing.
                "allow_structured_metadata": True,
                "volume_enabled": True,
                "discover_log_levels": True,
                # A laptop that has been asleep replays old timestamps on wake.
                "reject_old_samples": False,
            },
            # Off by default, and the only source for Logs Drilldown's Patterns
            # tab -- without it that tab is permanently empty and /patterns
            # 404s. In-memory ring, matching the single-binary topology above.
            "pattern_ingester": {"enabled": True},
            "compactor": {
                "working_directory": "/loki/compactor",
                # Without this, retention_period above is inert.
                "retention_enabled": True,
                "delete_request_store": "filesystem",
            },
            "analytics": {"reporting_enabled": False},
        },
        default_flow_style=False,
        sort_keys=False,
    )


def _logs_dashboard() -> str:
    """Return the starter "local-dev logs" dashboard as a JSON string."""
    loki_ds = {"type": "loki", "uid": "loki"}
    stream = '{namespace=~"$namespace"} |= "$search"'
    return json.dumps(
        {
            "uid": "local-dev-logs",
            "title": "local-dev logs",
            "tags": ["local-dev"],
            "timezone": "browser",
            "schemaVersion": 39,
            "time": {"from": "now-1h", "to": "now"},
            "refresh": "30s",
            "templating": {
                "list": [
                    {
                        "name": "namespace",
                        "label": "Namespace",
                        "type": "query",
                        "datasource": loki_ds,
                        "query": "label_values(namespace)",
                        "refresh": 1,
                        "includeAll": True,
                        "multi": True,
                        "current": {"text": "All", "value": "$__all"},
                    },
                    {
                        "name": "search",
                        "label": "Search",
                        "type": "textbox",
                        "query": "",
                        "current": {"text": "", "value": ""},
                    },
                ]
            },
            "panels": [
                {
                    "id": 1,
                    "type": "timeseries",
                    "title": "Log rate by namespace",
                    "datasource": loki_ds,
                    "gridPos": {"h": 7, "w": 24, "x": 0, "y": 0},
                    "targets": [
                        {
                            "refId": "A",
                            "datasource": loki_ds,
                            "expr": f"sum by (namespace) (rate({stream} [$__auto]))",
                        }
                    ],
                },
                {
                    "id": 2,
                    "type": "logs",
                    "title": "Logs",
                    "datasource": loki_ds,
                    "gridPos": {"h": 17, "w": 24, "x": 0, "y": 7},
                    "options": {
                        "showTime": True,
                        "wrapLogMessage": True,
                        "enableLogDetails": True,
                        "sortOrder": "Descending",
                    },
                    "targets": [{"refId": "A", "datasource": loki_ds, "expr": stream}],
                },
            ],
        }
    )


def _create_loki(
    _k8s: Callable[..., ResourceOptions],
    operations_ns: k8s.core.v1.Namespace,
    retention_period: str,
) -> k8s.apps.v1.Deployment:
    """Deploy single-binary Loki backed by a filesystem PVC."""
    config = _loki_config(retention_period)

    config_cm = k8s.core.v1.ConfigMap(
        "loki-config",
        metadata={"name": "loki-config", "namespace": NAMESPACE},
        data={"config.yaml": config},
        opts=_k8s(parent=operations_ns),
    )

    # A PVC rather than the emptyDir other local-dev services use: a retention
    # window is meaningless if a pod restart wipes the log store.
    pvc = k8s.core.v1.PersistentVolumeClaim(
        "loki-data",
        metadata={
            "name": "loki-data",
            "namespace": NAMESPACE,
            # k3d's local-path class binds WaitForFirstConsumer, so the claim
            # stays Pending until a pod mounts it. Without skipAwait, Pulumi
            # blocks waiting for Bound and the Deployment that would unblock it
            # is never created.
            "annotations": {"pulumi.com/skipAwait": "true"},
        },
        spec={
            "accessModes": ["ReadWriteOnce"],
            "resources": {"requests": {"storage": "5Gi"}},
        },
        opts=_k8s(parent=operations_ns),
    )

    deployment = k8s.apps.v1.Deployment(
        "loki",
        metadata={"name": "loki", "namespace": NAMESPACE},
        spec={
            "replicas": 1,
            "selector": {"matchLabels": {"app": "loki"}},
            # The PVC is ReadWriteOnce, so the old pod must go before the new
            # one can attach.
            "strategy": {"type": "Recreate"},
            "template": {
                "metadata": {
                    "labels": {"app": "loki"},
                    # Roll the pod when the retention period (or any other
                    # config) changes; a mounted ConfigMap update alone would
                    # not restart Loki.
                    "annotations": {"checksum/config": _checksum(config)},
                },
                "spec": {
                    "securityContext": {"fsGroup": 10001},
                    "containers": [
                        {
                            "name": "loki",
                            "image": LOKI_IMAGE,
                            "args": ["-config.file=/etc/loki/config.yaml"],
                            "ports": [{"containerPort": 3100, "name": "http"}],
                            "readinessProbe": {
                                "httpGet": {"path": "/ready", "port": 3100},
                                "initialDelaySeconds": 20,
                                "periodSeconds": 10,
                                "failureThreshold": 12,
                            },
                            "resources": {"limits": {"memory": "512Mi"}},
                            "volumeMounts": [
                                {"name": "config", "mountPath": "/etc/loki"},
                                {"name": "data", "mountPath": "/loki"},
                            ],
                        }
                    ],
                    "volumes": [
                        {"name": "config", "configMap": {"name": "loki-config"}},
                        {
                            "name": "data",
                            "persistentVolumeClaim": {"claimName": "loki-data"},
                        },
                    ],
                },
            },
        },
        opts=_k8s(parent=operations_ns, depends_on=[config_cm, pvc]),
    )

    k8s.core.v1.Service(
        "loki-svc",
        metadata={"name": "loki", "namespace": NAMESPACE},
        spec={
            "selector": {"app": "loki"},
            "ports": [{"name": "http", "port": 3100, "targetPort": 3100}],
        },
        opts=_k8s(parent=deployment),
    )

    return deployment


def _create_alloy(
    _k8s: Callable[..., ResourceOptions],
    operations_ns: k8s.core.v1.Namespace,
    loki: k8s.apps.v1.Deployment,
) -> k8s.apps.v1.DaemonSet:
    """Deploy the Alloy DaemonSet that tails pod logs into Loki."""
    service_account = k8s.core.v1.ServiceAccount(
        "alloy-sa",
        metadata={"name": "alloy", "namespace": NAMESPACE},
        opts=_k8s(parent=operations_ns),
    )

    cluster_role = k8s.rbac.v1.ClusterRole(
        "alloy-cr",
        metadata={"name": "alloy-local-dev"},
        rules=[
            {
                "api_groups": [""],
                "resources": ["pods", "nodes", "namespaces"],
                "verbs": ["get", "list", "watch"],
            }
        ],
        opts=_k8s(parent=operations_ns),
    )

    cluster_role_binding = k8s.rbac.v1.ClusterRoleBinding(
        "alloy-crb",
        metadata={"name": "alloy-local-dev"},
        role_ref={
            "api_group": "rbac.authorization.k8s.io",
            "kind": "ClusterRole",
            "name": "alloy-local-dev",
        },
        subjects=[{"kind": "ServiceAccount", "name": "alloy", "namespace": NAMESPACE}],
        opts=_k8s(parent=cluster_role, depends_on=[service_account]),
    )

    config_cm = k8s.core.v1.ConfigMap(
        "alloy-config",
        metadata={"name": "alloy-config", "namespace": NAMESPACE},
        data={"config.alloy": ALLOY_CONFIG},
        opts=_k8s(parent=operations_ns),
    )

    daemonset = k8s.apps.v1.DaemonSet(
        "alloy",
        metadata={"name": "alloy", "namespace": NAMESPACE},
        spec={
            "selector": {"matchLabels": {"app": "alloy"}},
            "template": {
                "metadata": {
                    "labels": {"app": "alloy"},
                    "annotations": {"checksum/config": _checksum(ALLOY_CONFIG)},
                },
                "spec": {
                    "serviceAccountName": "alloy",
                    # The k3d server node carries a control-plane taint but runs
                    # pods too, so its logs would otherwise be missed.
                    "tolerations": [{"operator": "Exists"}],
                    "containers": [
                        {
                            "name": "alloy",
                            "image": ALLOY_IMAGE,
                            "args": [
                                "run",
                                "/etc/alloy/config.alloy",
                                "--storage.path=/var/lib/alloy/data",
                                "--server.http.listen-addr=0.0.0.0:12345",
                            ],
                            # Container log files under /var/log/pods are
                            # root-owned.
                            "securityContext": {"runAsUser": 0, "runAsGroup": 0},
                            "ports": [
                                {"containerPort": 12345, "name": "http"},
                                {"containerPort": 4317, "name": "otlp-grpc"},
                                {"containerPort": 4318, "name": "otlp-http"},
                            ],
                            "resources": {"limits": {"memory": "192Mi"}},
                            "volumeMounts": [
                                {"name": "config", "mountPath": "/etc/alloy"},
                                {
                                    "name": "varlog",
                                    "mountPath": "/var/log",
                                    "readOnly": True,
                                },
                                {"name": "data", "mountPath": "/var/lib/alloy/data"},
                            ],
                        }
                    ],
                    "volumes": [
                        {"name": "config", "configMap": {"name": "alloy-config"}},
                        {"name": "varlog", "hostPath": {"path": "/var/log"}},
                        # loki.source.file records its read offsets under the
                        # storage path, so this has to outlive the pod. On an
                        # emptyDir every DaemonSet replacement loses every
                        # position and, since tail_from_end defaults to false,
                        # Alloy re-reads each still-present pod log from byte
                        # zero -- duplicate lines in Loki and a load spike on
                        # every restart. hostPath keeps the offsets per node,
                        # which is the shape a DaemonSet wants.
                        {
                            "name": "data",
                            "hostPath": {
                                "path": "/var/lib/alloy/data",
                                "type": "DirectoryOrCreate",
                            },
                        },
                    ],
                },
            },
        },
        opts=_k8s(
            parent=operations_ns,
            depends_on=[config_cm, cluster_role_binding, loki],
        ),
    )

    k8s.core.v1.Service(
        "alloy-svc",
        metadata={"name": "alloy", "namespace": NAMESPACE},
        spec={
            "selector": {"app": "alloy"},
            "ports": [
                {"name": "otlp-grpc", "port": 4317, "targetPort": 4317},
                {"name": "otlp-http", "port": 4318, "targetPort": 4318},
            ],
        },
        opts=_k8s(parent=daemonset),
    )

    return daemonset


def _create_grafana(
    _k8s: Callable[..., ResourceOptions],
    operations_ns: k8s.core.v1.Namespace,
    apisix_release: k8s.helm.v3.Release,
    tls_secret_ops: k8s.core.v1.Secret,
    grafana_hostname: str,
    loki: k8s.apps.v1.Deployment,
) -> k8s.apps.v1.Deployment:
    """Deploy Grafana with a provisioned Loki datasource and starter dashboard."""
    # Production does not provision datasources -- Grafana Cloud supplies them,
    # which is why grafana_alerting/dashboards/datasources.py only holds refs.
    # Self-hosted needs the real thing.
    datasources_cm = k8s.core.v1.ConfigMap(
        "grafana-datasources",
        metadata={"name": "grafana-datasources", "namespace": NAMESPACE},
        data={
            "loki.yaml": pyyaml.safe_dump(
                {
                    "apiVersion": 1,
                    "datasources": [
                        {
                            "name": "Loki",
                            "type": "loki",
                            "uid": "loki",
                            "access": "proxy",
                            "url": LOKI_URL,
                            "isDefault": True,
                        }
                    ],
                },
                default_flow_style=False,
                sort_keys=False,
            )
        },
        opts=_k8s(parent=operations_ns),
    )

    dashboards_cm = k8s.core.v1.ConfigMap(
        "grafana-dashboards",
        metadata={"name": "grafana-dashboards", "namespace": NAMESPACE},
        data={
            "provider.yaml": pyyaml.safe_dump(
                {
                    "apiVersion": 1,
                    "providers": [
                        {
                            "name": "local-dev",
                            "type": "file",
                            "allowUiUpdates": True,
                            # Deliberately not under /var/lib/grafana: that
                            # path is the PVC mount, and nesting a ConfigMap
                            # mount inside it depends on kubelet mount
                            # ordering.
                            "options": {"path": "/etc/grafana/dashboards"},
                        }
                    ],
                },
                default_flow_style=False,
                sort_keys=False,
            ),
            "local-dev-logs.json": _logs_dashboard(),
        },
        opts=_k8s(parent=operations_ns),
    )

    pvc = k8s.core.v1.PersistentVolumeClaim(
        "grafana-data",
        metadata={
            "name": "grafana-data",
            "namespace": NAMESPACE,
            # See the loki-data claim: WaitForFirstConsumer would otherwise
            # deadlock Pulumi's Bound await.
            "annotations": {"pulumi.com/skipAwait": "true"},
        },
        spec={
            "accessModes": ["ReadWriteOnce"],
            "resources": {"requests": {"storage": "1Gi"}},
        },
        opts=_k8s(parent=operations_ns),
    )

    deployment = k8s.apps.v1.Deployment(
        "grafana",
        metadata={"name": "grafana", "namespace": NAMESPACE},
        spec={
            "replicas": 1,
            "selector": {"matchLabels": {"app": "grafana"}},
            "strategy": {"type": "Recreate"},
            "template": {
                "metadata": {"labels": {"app": "grafana"}},
                "spec": {
                    "securityContext": {"fsGroup": 472},
                    "containers": [
                        {
                            "name": "grafana",
                            "image": GRAFANA_IMAGE,
                            "ports": [{"containerPort": 3000, "name": "http"}],
                            "env": [
                                {
                                    "name": "GF_SERVER_ROOT_URL",
                                    "value": f"https://{grafana_hostname}",
                                },
                                # Local-only cluster: skip the login round-trip
                                # entirely and land straight in Explore.
                                {
                                    "name": "GF_AUTH_ANONYMOUS_ENABLED",
                                    "value": "true",
                                },
                                {
                                    "name": "GF_AUTH_ANONYMOUS_ORG_ROLE",
                                    "value": "Admin",
                                },
                                {"name": "GF_AUTH_BASIC_ENABLED", "value": "false"},
                                {
                                    "name": "GF_AUTH_DISABLE_LOGIN_FORM",
                                    "value": "true",
                                },
                                {
                                    "name": "GF_ANALYTICS_REPORTING_ENABLED",
                                    "value": "false",
                                },
                                {
                                    "name": "GF_ANALYTICS_CHECK_FOR_UPDATES",
                                    "value": "false",
                                },
                                {"name": "GF_USERS_DEFAULT_THEME", "value": "dark"},
                                {
                                    "name": "GF_PLUGINS_PREINSTALL",
                                    "value": GRAFANA_PLUGINS,
                                },
                                # See GRAFANA_PLUGINS: the default async
                                # install is why Logs Drilldown was missing
                                # from the nav on a first boot.
                                {
                                    "name": "GF_PLUGINS_PREINSTALL_ASYNC",
                                    "value": "false",
                                },
                            ],
                            "readinessProbe": {
                                "httpGet": {"path": "/api/health", "port": 3000},
                                "initialDelaySeconds": 15,
                                # Grafana does not listen until the synchronous
                                # plugin install finishes, so the first boot on
                                # an empty PVC has a ~13MB download in front of
                                # it. 5 minutes covers a slow connection.
                                "periodSeconds": 10,
                                "failureThreshold": 30,
                            },
                            "resources": {"limits": {"memory": "256Mi"}},
                            "volumeMounts": [
                                {
                                    "name": "datasources",
                                    "mountPath": (
                                        "/etc/grafana/provisioning/datasources"
                                    ),
                                },
                                {
                                    "name": "dashboard-provider",
                                    "mountPath": (
                                        "/etc/grafana/provisioning/dashboards"
                                    ),
                                },
                                {
                                    "name": "dashboards",
                                    "mountPath": "/etc/grafana/dashboards",
                                },
                                {"name": "data", "mountPath": "/var/lib/grafana"},
                            ],
                        }
                    ],
                    "volumes": [
                        {
                            "name": "datasources",
                            "configMap": {"name": "grafana-datasources"},
                        },
                        {
                            "name": "dashboard-provider",
                            "configMap": {
                                "name": "grafana-dashboards",
                                "items": [
                                    {
                                        "key": "provider.yaml",
                                        "path": "provider.yaml",
                                    }
                                ],
                            },
                        },
                        {
                            "name": "dashboards",
                            "configMap": {
                                "name": "grafana-dashboards",
                                "items": [
                                    {
                                        "key": "local-dev-logs.json",
                                        "path": "local-dev-logs.json",
                                    }
                                ],
                            },
                        },
                        {
                            "name": "data",
                            "persistentVolumeClaim": {"claimName": "grafana-data"},
                        },
                    ],
                },
            },
        },
        opts=_k8s(
            parent=operations_ns,
            depends_on=[datasources_cm, dashboards_cm, pvc, loki],
        ),
    )

    k8s.core.v1.Service(
        "grafana-svc",
        metadata={"name": "grafana", "namespace": NAMESPACE},
        spec={
            "selector": {"app": "grafana"},
            "ports": [{"name": "http", "port": 3000, "targetPort": 3000}],
        },
        opts=_k8s(parent=deployment),
    )

    k8s.apiextensions.CustomResource(
        "grafana-apisix-route",
        api_version="apisix.apache.org/v2",
        kind="ApisixRoute",
        metadata={"name": "grafana-route", "namespace": NAMESPACE},
        spec={
            "ingressClassName": "apache-apisix",
            "http": [
                {
                    "name": "grafana",
                    "match": {"hosts": [grafana_hostname], "paths": ["/*"]},
                    "backends": [{"serviceName": "grafana", "servicePort": 3000}],
                    # Explore's live tail streams over a websocket.
                    "websocket": True,
                }
            ],
        },
        opts=_k8s(parent=operations_ns, depends_on=[apisix_release, deployment]),
    )

    k8s.apiextensions.CustomResource(
        "grafana-apisix-tls",
        api_version="apisix.apache.org/v2",
        kind="ApisixTls",
        metadata={"name": "grafana-tls", "namespace": NAMESPACE},
        spec={
            "ingressClassName": "apache-apisix",
            "hosts": [grafana_hostname],
            "secret": {"name": "local-dev-tls", "namespace": NAMESPACE},
        },
        opts=_k8s(parent=operations_ns, depends_on=[apisix_release, tls_secret_ops]),
    )

    return deployment


def create_observability(
    _k8s: Callable[..., ResourceOptions],
    operations_ns: k8s.core.v1.Namespace,
    apisix_release: k8s.helm.v3.Release,
    tls_secret_ops: k8s.core.v1.Secret,
    grafana_hostname: str,
    log_retention_period: str = "168h",
) -> ObservabilityResources:
    """Deploy Loki, Alloy, and Grafana into the operations namespace.

    Web UI is exposed at https://{grafana_hostname}; logs from every pod in the
    cluster are searchable there for *log_retention_period*.
    """
    retention_period = validate_retention_period(log_retention_period)

    loki = _create_loki(_k8s, operations_ns, retention_period)
    alloy = _create_alloy(_k8s, operations_ns, loki)
    grafana = _create_grafana(
        _k8s,
        operations_ns,
        apisix_release=apisix_release,
        tls_secret_ops=tls_secret_ops,
        grafana_hostname=grafana_hostname,
        loki=loki,
    )

    return ObservabilityResources(loki=loki, alloy=alloy, grafana=grafana)
