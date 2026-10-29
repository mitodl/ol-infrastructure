# ruff: noqa: E501
"""What the shared APISIX gateway is in every environment it runs in.

The APISIX Helm values, the Gateway API ``GatewayClass`` and ``Gateway``, and
the identity-header-strip global rule live here. Everything that depends on
where the gateway runs (replica count, how the Service is exposed, image
source, trusted proxies, trusted CA) arrives as an ``ApisixGatewayConfig``.
``setup_apisix`` in apisix_official.py builds one from the EKS stack config;
local-dev's ``local-dev/infra/modules/ingress.py`` builds one for k3d.

Nothing in this module may read Pulumi config, a stack reference, or a cloud
API, so that importing it needs no provider.
"""

import textwrap
from pathlib import Path
from typing import Any, Literal

from pulumi import Output
from pydantic import BaseModel, ConfigDict

from bridge.lib.constants import mit_learn_session_cookie_name
from bridge.lib.magic_numbers import DEFAULT_HTTPS_PORT
from ol_infrastructure.components.services.apisix import (
    OIDC_RECOVERY_SHARED_DICT,
    OIDC_RECOVERY_SHARED_DICT_SIZE,
    gateway_global_pre_function_plugin,
)

APISIX_CHART_REPOSITORY = "https://apache.github.io/apisix-helm-chart"
APISIX_RELEASE_NAME = "apache-apisix"
APISIX_INGRESS_CLASS = "apache-apisix"
APISIX_NAMESPACE = "operations"
# The chart's gatewayProxy.createDefault names it "<release>-config".
APISIX_GATEWAY_PROXY_NAME = f"{APISIX_RELEASE_NAME}-config"
GATEWAY_NAME = "apisix"
GATEWAY_ERROR_PAGES_CONFIGMAP_NAME = "apache-apisix-error-pages"
# Renaming the rule replaces it (delete_before_replace), leaving a window with
# no strip at all, so it keeps this name although it now does more.
IDENTITY_HEADER_STRIP_RULE_NAME = "identity-header-strip"
DEFAULT_CPU_REQUEST = "100m"
DEFAULT_MEMORY_REQUEST = "400Mi"

_ERROR_PAGES_DIR = Path(__file__).resolve().parent / "error_pages"

# TLS 1.2 cipher suites the gateway offers (TLS 1.3 suites are not affected by
# ssl_ciphers).  This is Mozilla's "intermediate" profile, and also the first
# nine entries of the chart's own default -- the chart default goes on to add
# CBC suites, suites without forward secrecy and DES-CBC3-SHA (hq#14079).
# The DHE suites are inert without an ssl_dhparam and are kept only so this
# stays a verbatim copy of the profile.
# Ref: https://wiki.mozilla.org/Security/Server_Side_TLS
GATEWAY_SSL_CIPHERS = (
    "ECDHE-ECDSA-AES128-GCM-SHA256:"
    "ECDHE-RSA-AES128-GCM-SHA256:"
    "ECDHE-ECDSA-AES256-GCM-SHA384:"
    "ECDHE-RSA-AES256-GCM-SHA384:"
    "ECDHE-ECDSA-CHACHA20-POLY1305:"
    "ECDHE-RSA-CHACHA20-POLY1305:"
    "DHE-RSA-AES128-GCM-SHA256:"
    "DHE-RSA-AES256-GCM-SHA384:"
    "DHE-RSA-CHACHA20-POLY1305"
)

# APISIX's default enabled-plugins list for the version shipped by the pinned
# chart (chart 2.17.x => APISIX 3.18.x), taken from
# https://github.com/apache/apisix/blob/3.18.0/apisix/cli/config.lua
#
# Setting ``apisix.plugins`` in the Helm values REPLACES this default list
# rather than extending it, so the full list must be reproduced here.  We do
# this solely to add ``opentelemetry`` (which is NOT enabled by default) without
# silently dropping any plugin that applications already rely on.
#
# ``server-info`` is absent here because upstream comments it out of its own
# default list ("deprecated and will be removed in a future release") in both
# 3.17 and 3.18.  This list is the active set, so it stays out -- no local
# exception to carry forward on the next upgrade.
#
# 3.18 adds ``ai-cache``, ``ai-lakera-guard`` and ``ldap-auth-advanced`` to the
# defaults and removes none.
#
# IMPORTANT: revisit this list on every APISIX major/minor upgrade.  If it
# drifts from the APISIX default for the running version, plugins omitted here
# will stop loading.  ``opentelemetry`` is appended at the end below.
APISIX_DEFAULT_PLUGINS_3_18: list[str] = [
    "real-ip",
    "ai",
    "client-control",
    "proxy-buffering",
    "proxy-control",
    "request-id",
    "zipkin",
    "ext-plugin-pre-req",
    "fault-injection",
    "mocking",
    "serverless-pre-function",
    "cors",
    "ip-restriction",
    "ua-restriction",
    "referer-restriction",
    "csrf",
    "uri-blocker",
    "request-validation",
    "chaitin-waf",
    "multi-auth",
    "openid-connect",
    "saml-auth",
    "cas-auth",
    "authz-casbin",
    "authz-casdoor",
    "wolf-rbac",
    "ldap-auth-advanced",
    "ldap-auth",
    "hmac-auth",
    "basic-auth",
    "jwt-auth",
    "jwe-decrypt",
    "key-auth",
    "dingtalk-auth",
    "feishu-auth",
    "acl",
    "consumer-restriction",
    "attach-consumer-label",
    "forward-auth",
    "opa",
    "authz-keycloak",
    "data-mask",
    "proxy-cache",
    "body-transformer",
    "ai-prompt-template",
    "ai-prompt-decorator",
    "ai-prompt-guard",
    "ai-rag",
    "ai-rate-limiting",
    "ai-proxy-multi",
    "ai-proxy",
    "ai-cache",
    "ai-aws-content-moderation",
    "ai-aliyun-content-moderation",
    "ai-lakera-guard",
    "proxy-mirror",
    "graphql-proxy-cache",
    "proxy-rewrite",
    "workflow",
    "api-breaker",
    "graphql-limit-count",
    "limit-conn",
    "limit-count",
    "limit-req",
    "gzip",
    "traffic-label",
    "traffic-split",
    "redirect",
    "response-rewrite",
    "oas-validator",
    "mcp-bridge",
    "degraphql",
    "kafka-proxy",
    "grpc-transcode",
    "grpc-web",
    "http-dubbo",
    "public-api",
    "prometheus",
    "datadog",
    "lago",
    "loki-logger",
    "elasticsearch-logger",
    "echo",
    "loggly",
    "http-logger",
    "splunk-hec-logging",
    "skywalking-logger",
    "google-cloud-logging",
    "sls-logger",
    "tcp-logger",
    "kafka-logger",
    "rocketmq-logger",
    "syslog",
    "udp-logger",
    "file-logger",
    "clickhouse-logger",
    "tencent-cloud-cls",
    "inspect",
    "example-plugin",
    "aws-lambda",
    "azure-functions",
    "openwhisk",
    "openfunction",
    "serverless-post-function",
    "ext-plugin-post-req",
    "ext-plugin-post-resp",
    "ai-request-rewrite",
]

# Full enabled-plugins list applied via the Helm chart: APISIX defaults plus the
# opentelemetry plugin, which emits OTLP traces (not metrics) for every request.
APISIX_ENABLED_PLUGINS: list[str] = [
    *APISIX_DEFAULT_PLUGINS_3_18,
    "opentelemetry",
]


def _double_memory_quantity(quantity: str) -> str:
    """Double a Kubernetes memory quantity, preserving its unit suffix.

    Used to derive a default memory limit with burst headroom above the
    configured request when no explicit limit override is supplied.
    Supports the binary (Ki/Mi/Gi/Ti) and decimal (K/M/G/T) suffixes valid
    for a Kubernetes memory quantity, and fractional prefixes (e.g.
    "1.5Gi"). Pi/Ei and exponential notation are not supported -- not a
    realistic unit for a gateway memory request/limit -- and raise the
    same clear error as any other unrecognized suffix.
    """
    for suffix in ("Ki", "Mi", "Gi", "Ti", "K", "M", "G", "T"):
        if quantity.endswith(suffix):
            value = float(quantity[: -len(suffix)]) * 2
            return f"{int(value) if value.is_integer() else value}{suffix}"
    msg = f"Unsupported memory quantity suffix: {quantity!r}"
    raise ValueError(msg)


class ApisixHighAvailability(BaseModel):
    """Run the gateway as an autoscaled fleet rather than a single pod.

    Turns on the HPA and everything that only means something with more than
    one pod: zone and node spreading, the drain-first rollout, the preStop
    drain, and the PodDisruptionBudget. Left unset, the gateway runs one pod
    with the chart's surge-first rollout, which keeps serving through a
    restart; drain-first on a single pod would take the gateway down for the
    whole of the new pod's startup.
    """

    min_replicas: int
    max_replicas: int
    target_cpu_utilization_percentage: int


class ApisixGatewayService(BaseModel):
    """How the gateway Service is exposed."""

    model_config = ConfigDict(arbitrary_types_allowed=True, extra="forbid")

    type: Literal["LoadBalancer", "NodePort"]
    annotations: dict[str, Any] | None = None
    http_node_port: int | None = None
    tls_node_port: int | None = None


class ApisixTrustedCA(BaseModel):
    """A Secret holding the CA bundle APISIX verifies upstream TLS against.

    Setting it replaces APISIX's default (the system store), so the bundle has
    to carry public roots as well as any private one.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True, extra="forbid")

    secret_name: str | Output[str]
    filename: str


class ApisixGatewayConfig(BaseModel):
    """Everything about the gateway that depends on where it runs."""

    model_config = ConfigDict(arbitrary_types_allowed=True, extra="forbid")

    env_suffix: str
    admin_key: str | Output[str]
    viewer_key: str | Output[str]
    service: ApisixGatewayService
    high_availability: ApisixHighAvailability | None = None
    ingress_controller_replicas: int = 1
    cpu_request: str = DEFAULT_CPU_REQUEST
    memory_request: str = DEFAULT_MEMORY_REQUEST
    # Defaults to double memory_request; see the resources block below.
    memory_limit: str | None = None
    # Unset uses the chart's own image.
    image_repository: str | None = None
    tolerations: list[dict[str, str]] = []
    # Labels for the objects the chart creates. When set, they also replace
    # the chart's selector labels, which is immutable on the Deployment: an
    # existing release cannot change between set and unset without the
    # Deployment being recreated.
    labels: dict[str, str] | None = None
    # Source addresses whose X-Forwarded-For APISIX believes (e.g. Fastly).
    trusted_addresses: list[str] | Output[list[str]] | None = None
    # host:port of an OTLP/HTTP collector to export traces to; None turns
    # tracing off. The opentelemetry plugin only speaks binary OTLP over HTTP
    # and appends /v1/traces itself.
    otel_collector_address: str | None = None
    # Needs the Prometheus operator's ServiceMonitor CRD.
    service_monitor_enabled: bool = False
    # The ingress controller's validating webhook checks a route's
    # plugin_config_name against its informer cache, so a PluginConfig and a
    # route naming it that are created together can have the route denied
    # until the cache catches up. Deployed stacks are safe today because their
    # PluginConfigs already exist; OLApisixRoute names the PluginConfig by a
    # plain string, so Pulumi does not order a new pair either.
    admission_webhook_enabled: bool = True
    trusted_ca: ApisixTrustedCA | None = None
    pod_annotations: dict[str, str] | None = None

    @property
    def otel_tracing_enabled(self) -> bool:
        return self.otel_collector_address is not None

    @property
    def effective_memory_limit(self) -> str:
        """Return memory_limit, or double memory_request when it is unset."""
        return self.memory_limit or _double_memory_quantity(self.memory_request)


def gateway_error_pages() -> dict[str, str]:
    """Return the ConfigMap data the gateway's ``error_page`` directive serves.

    The ConfigMap must be named ``GATEWAY_ERROR_PAGES_CONFIGMAP_NAME`` in the
    gateway's namespace; ``apisix_helm_values`` mounts it into every pod.
    """
    return {
        "gateway_error.html": (_ERROR_PAGES_DIR / "gateway_error.html").read_text(
            encoding="utf-8"
        )
    }


def _otel_plugin_metadata(env_suffix: str, collector_address: str) -> dict[str, Any]:
    # Collector/resource/batch settings for the opentelemetry plugin.  In APISIX
    # 3.14 the plugin reads these from *plugin_metadata*, NOT plugin_attr: its
    # ``rewrite`` phase calls ``plugin.plugin_metadata("opentelemetry")`` and, if
    # that is nil, logs "plugin_metadata is required for opentelemetry plugin to
    # working properly" and returns *without creating or exporting a span* (see
    # apisix/plugins/opentelemetry.lua).  The ingress controller delivers this to
    # the data plane via the GatewayProxy ``pluginMetadata`` field (rendered into
    # the standalone config's ``plugin_metadata`` over the Admin API) -- see the
    # ingress-controller.gatewayProxy values below.  ``trace_id_source: random``
    # yields spec-compliant 16-byte trace ids; "x-request-id" would pass the
    # request-id UUID verbatim, which Tempo drops as malformed.
    return {
        "trace_id_source": "random",
        "resource": {
            "service.name": "apisix",
            "deployment.environment": env_suffix.lower(),
        },
        "collector": {
            "address": collector_address,
            "request_timeout": 3,
        },
        "batch_span_processor": {
            "drop_on_queue_full": False,
            "max_queue_size": 1024,
            "batch_timeout": 2,
            "inactive_timeout": 1,
            "max_export_batch_size": 16,
        },
        "set_ngx_var": False,
    }


def _gateway_service_values(config: ApisixGatewayConfig) -> dict[str, Any]:
    service = config.service
    http: dict[str, Any] = {
        "enabled": True,
        "servicePort": 80,
        "containerPort": 9080,
    }
    tls: dict[str, Any] = {
        "enabled": True,
        "servicePort": 443,
        "containerPort": 9443,
    }
    if service.http_node_port is not None:
        http["nodePort"] = service.http_node_port
    if service.tls_node_port is not None:
        tls["nodePort"] = service.tls_node_port
    values: dict[str, Any] = {"type": service.type}
    if service.annotations is not None:
        values["annotations"] = service.annotations
    values["http"] = http
    values["tls"] = tls
    if config.labels is not None:
        values["labelsOverride"] = {
            **config.labels,
            "app.kubernetes.io/name": APISIX_RELEASE_NAME,
            "app.kubernetes.io/component": "gateway",
        }
    return values


def _spread_across_zones_and_nodes(match_labels: dict[str, str]) -> dict[str, Any]:
    # Soft constraints (ScheduleAnyway + preferred anti-affinity): they improve
    # distribution where possible but do not block scheduling when the
    # topology cannot be perfectly balanced.
    return {
        "topologySpreadConstraints": [
            {
                "maxSkew": 1,
                "topologyKey": topology_key,
                "whenUnsatisfiable": "ScheduleAnyway",
                "labelSelector": {"matchLabels": match_labels},
            }
            for topology_key in (
                "topology.kubernetes.io/zone",
                "kubernetes.io/hostname",
            )
        ],
        "affinity": {
            "podAntiAffinity": {
                "preferredDuringSchedulingIgnoredDuringExecution": [
                    {
                        "weight": 100,
                        "podAffinityTerm": {
                            "labelSelector": {"matchLabels": match_labels},
                            "topologyKey": "kubernetes.io/hostname",
                        },
                    }
                ]
            }
        },
    }


def _high_availability_values(ha: ApisixHighAvailability) -> dict[str, Any]:
    return {
        "autoscaling": {
            "enabled": True,
            "minReplicas": ha.min_replicas,
            "maxReplicas": ha.max_replicas,
            "targetCPUUtilizationPercentage": ha.target_cpu_utilization_percentage,
        },
        # Spread gateway pods across availability zones and nodes to reduce
        # the likelihood that an AZ-level failure takes out multiple pods and
        # to lower the probability that a rolling update places consecutive pods
        # on the same node (which shrinks the window where a pod with a stale
        # services_conf_version can block ADC sidecar syncs on a freshly
        # restarted ingress controller).
        **_spread_across_zones_and_nodes(
            {
                "app.kubernetes.io/name": APISIX_RELEASE_NAME,
                "app.kubernetes.io/component": "gateway",
            }
        ),
        # --- Rolling Update Strategy and Pod Lifecycle ---
        # maxSurge: 0 / maxUnavailable: 1 (drain-first) ensures that an old
        # pod -- which may hold a high services_conf_version accumulated over
        # days of ADC syncs -- is fully removed from endpoint slices BEFORE a
        # replacement pod starts.  This eliminates the race where a freshly
        # restarted ingress-controller ADC sidecar resolves a mix of old (high-
        # version) and new (version-0) APISix pod IPs and gets a
        # "services_conf_version must be >= N" rejection from the old pod.
        #
        # The tradeoff is a brief capacity dip to (desired-1) pods during each
        # step, which the PDB (maxUnavailable: 1) already accounts for.
        #
        # minReadySeconds: 30 forces Kubernetes to observe the new pod as
        # healthy for 30 s before advancing to the next rollout step, preventing
        # a bad image from racing through the entire fleet undetected.
        "updateStrategy": {
            "type": "RollingUpdate",
            "rollingUpdate": {
                "maxSurge": 0,
                "maxUnavailable": 1,
            },
        },
        "minReadySeconds": 30,
        # Give in-flight requests up to 120 s to complete after the pod is
        # removed from the NLB target group and endpoint slice.  The preStop
        # sleep bridges the gap between endpoint-slice propagation (< 5 s) and
        # the last long-lived connection draining; 90 s < 120 s leaves a 30 s
        # hard-kill buffer so the kubelet never has to SIGKILL the process.
        "terminationGracePeriodSeconds": 120,
        "lifecycle": {
            "preStop": {
                "exec": {
                    "command": ["/bin/sh", "-c", "sleep 90"],
                }
            }
        },
        # maxUnavailable: 1 scales correctly as the HPA grows the replica count
        # above the 3-pod floor: it always limits disruptions to at most one pod
        # at a time, regardless of the current scale.  minAvailable: 2 would
        # permit up to 3 simultaneous evictions at maxReplicas: 5, which could
        # cascade a VPA eviction, a node drain, and a rolling-update step into
        # a single disruptive event.
        "podDisruptionBudget": {
            "enabled": True,
            "maxUnavailable": 1,
        },
    }


def _ingress_controller_deployment_values(
    config: ApisixGatewayConfig,
) -> dict[str, Any]:
    values: dict[str, Any] = {
        "replicas": config.ingress_controller_replicas,
        "tolerations": config.tolerations,
        "resources": {
            "requests": {
                "cpu": "50m",
                "memory": "256Mi",
            },
            # The manager OOMs at 256Mi while decoding its initial
            # cache sync, and weekly peaks reach it on several
            # clusters. The chart gives it no env hook for
            # GOMEMLIMIT, so the ceiling needs real headroom.
            "limits": {
                "memory": "512Mi",
            },
        },
    }
    if config.high_availability is None:
        return values
    return {
        **values,
        # Surge-first rolling update (maxUnavailable: 0): a new IC pod
        # comes up and resolves *current* APISix endpoint IPs before the
        # old IC pod is terminated.  This means every new ADC sidecar
        # starts with a fresh, consistent view of the APISix fleet -- no
        # stale pod IPs that could produce services_conf_version conflicts.
        # minReadySeconds: 30 ensures the new IC pod has successfully
        # completed at least one ADC sync cycle before the old pod is
        # decommissioned.
        "strategy": {
            "type": "RollingUpdate",
            "rollingUpdate": {
                "maxSurge": 1,
                "maxUnavailable": 0,
            },
        },
        "minReadySeconds": 30,
        # Allow in-flight ADC sync operations and leader-election
        # transitions to complete gracefully before the pod exits.
        "terminationGracePeriodSeconds": 60,
        "lifecycle": {
            "preStop": {
                "exec": {
                    "command": ["/bin/sh", "-c", "sleep 20"],
                }
            }
        },
        # Spread ingress-controller pods across AZs and nodes to reduce
        # the likelihood of multiple replicas landing on the same node or
        # AZ.
        **_spread_across_zones_and_nodes(
            {"app.kubernetes.io/name": "apisix-ingress-controller"}
        ),
    }


def _nginx_values(env_suffix: str) -> dict[str, Any]:
    # Name of the OIDC session cookie the $oidc_session_bytes access-log field
    # (and the apisix_oidc_session_cookie_bytes dashboard built on it) measures.
    # There is one such variable per gateway but several OIDC-protected
    # applications behind it, so it tracks the largest and most-watched session
    # on the cluster: MIT Learn's, whose cookie is shared by learn-ai and
    # ol-analytics-api. Purely observational -- lua-resty-session 4.x ignores
    # these nginx variables, the cookie name itself is set per-application via
    # OLApisixOIDCConfig.oidc_session_cookie_name.
    session_cookie_name = mit_learn_session_cookie_name(env_suffix)
    return {
        "workerProcesses": "auto",
        "enableCPUAffinity": True,
        "workerConnections": "10620",
        # Counts OIDC login restarts per Keycloak session_state;
        # see oidc_gateway_pre_function_plugin.
        "customLuaSharedDicts": [
            {
                "name": OIDC_RECOVERY_SHARED_DICT,
                "size": OIDC_RECOVERY_SHARED_DICT_SIZE,
            },
        ],
        "logs": {
            "enableAccessLog": True,
            "accessLog": "/dev/stdout",
            "accessLogFormat": 'time_local="$time_local" '
            "body_bytes_sent=$body_bytes_sent "
            "bytes_sent=$bytes_sent "
            "client=$remote_addr "
            "host=$host "
            "remote_addr=$remote_addr "
            "request_id=$request_id "
            "request_length=$request_length "
            "request_method=$request_method "
            "request_time=$request_time "
            # Quoted because Loki's logfmt parser silently
            # DROPS a key whose unquoted value contains "=":
            # every URI with a query string ("?limit=12")
            # parsed to an empty request_uri, with no
            # __error__ raised to make the loss visible.  The
            # already-quoted $request on the same line kept
            # its full URI, which is what this mirrors.  Safe
            # with accessLogFormatEscape "default": nginx
            # escapes any literal quote in the URI as \".
            'request_uri="$request_uri" '
            "status=$status "
            "upstream_addr=$upstream_addr "
            "upstream_connect_time=$upstream_connect_time "
            "upstream_header_time=$upstream_header_time "
            "upstream_response_time=$upstream_response_time "
            "upstream_status=$upstream_status "
            'http_referer="$http_referer" '
            # Referer is subject to the page's referrer policy
            # and is often absent on cross-origin API calls;
            # Origin is sent on every CORS-mode request no
            # matter the policy, and Sec-Fetch-Site says
            # same-origin/cross-site/none without any URL.
            'http_origin="$http_origin" '
            'sec_fetch_site="$http_sec_fetch_site" '
            'http_user_agent="$http_user_agent" '
            "method=$request_method "
            'request="$request" '
            "cookie_bytes=$cookie_bytes "
            "cookie_count=$cookie_count "
            "oidc_session_bytes=$oidc_session_bytes "
            'cookie_names="$cookie_names" '
            'cookie_sizes="$cookie_sizes" '
            # What each client actually negotiated, so a cipher
            # change can be checked against real traffic.  Kept
            # last: grafana.py's Alloy regex expects the cookie_*
            # fields above to stay contiguous.
            "ssl_protocol=$ssl_protocol "
            "ssl_cipher=$ssl_cipher",
            "accessLogFormatEscape": "default",
            "errorLog": "/dev/stderr",
            "errorLogLevel": "warn",
        },
        "configurationSnippet": {
            "main": "",
            "httpStart": textwrap.dedent(
                """\
                client_header_buffer_size 8k;
                large_client_header_buffers 4 64k;

                # Pass a request to the next upstream node only
                # on an error connecting, sending the request
                # or reading the response header (e.g. a
                # refused connect to a pod that just died), not
                # on a timeout. The nginx default is "error
                # timeout", so on a route with endpoint
                # granularity a request that hit its read
                # timeout on an overloaded pod is re-sent to
                # another pod, adding load during an overload.
                # APISIX's ngx_tpl sets no
                # proxy_next_upstream of its own, so this
                # http-level value applies to every route.
                proxy_next_upstream error;

                # Explicit response-buffering tuning (2026-07-21
                # applications-production OOM incident): large,
                # slowly-consumed proxied responses (multi-MB
                # course CSVs, JupyterHub notebook_core.<hash>.js
                # bundles) were observed with request_time up to
                # 46.8s against upstream_response_time under
                # 0.1s -- i.e. held in a worker's memory for the
                # client's full slow-download duration rather
                # than overflowing to disk. proxy_buffering was
                # already on by default, but was left implicit
                # and unsized; making it explicit here so the
                # disk-overflow behavior is documented and
                # auditable rather than relying on upstream
                # chart/NGINX defaults silently changing.
                # proxy_buffers/proxy_buffer_size bound the
                # in-memory portion per request to ~72KB
                # (8k proxy_buffer_size + 8x8k proxy_buffers,
                # which are separate buffer pools -- the
                # former for the leading part of the response,
                # the latter for the rest); the remainder of
                # a large body spills to a temp
                # file (bounded by proxy_max_temp_file_size)
                # instead of growing worker RSS. If OOMs persist
                # after this change, the leading suspect is a
                # Lua body_filter-based plugin buffering the
                # full response into the Lua VM heap, which
                # bypasses these NGINX-level directives entirely
                # -- check enabled plugins on the affected
                # routes (course CSV / JupyterHub) for anything
                # that reads the response body.
                proxy_buffering on;
                proxy_buffer_size 8k;
                proxy_buffers 8 8k;
                proxy_busy_buffers_size 16k;
                proxy_max_temp_file_size 1024m;
                proxy_temp_file_write_size 64k;
                """
            ),
            "httpEnd": "",
            "httpSrv": textwrap.dedent(
                f"""\
                set $session_compressor zlib;
                set $session_name {session_cookie_name};

                set $cookie_bytes 0;
                set $cookie_count 0;
                set $oidc_session_bytes 0;
                set $cookie_names "";
                set $cookie_sizes "";

                # Parse the Cookie request header exactly once per request and
                # populate all telemetry variables.  APISix does not use
                # rewrite_by_lua_block in its server block so this is safe.
                rewrite_by_lua_block {{
                    local raw = ngx.var.http_cookie or ""
                    ngx.var.cookie_bytes = tostring(#raw)
                    if raw == "" then return end
                    local session_name = ngx.var.session_name or ""
                    local count = 0
                    local names = {{}}
                    local sizes = {{}}
                    local oidc_bytes = 0
                    for pair in (raw .. ";"):gmatch("([^;]+);") do
                        local name, val = pair:match("^%s*([^=]+)=(.*)")
                        if name then
                            local trimmed_name = name:match("^%s*(.-)%s*$")
                            if trimmed_name ~= "" then
                                count = count + 1
                                local trimmed_pair = pair:match("^%s*(.-)%s*$")
                                table.insert(names, trimmed_name)
                                table.insert(sizes, trimmed_name .. ":" .. tostring(#trimmed_pair))
                                if trimmed_name == session_name then
                                    oidc_bytes = #(trimmed_name .. "=" .. val)
                                end
                            end
                        end
                    end
                    ngx.var.cookie_count = tostring(count)
                    ngx.var.oidc_session_bytes = tostring(oidc_bytes)
                    ngx.var.cookie_names = table.concat(names, ",")
                    ngx.var.cookie_sizes = table.concat(sizes, ",")
                }}

                # Serve a branded error page for gateway-level errors (HTTP 400/431
                # from oversized request headers, HTTP 500 from OIDC plugin failures,
                # and other upstream/server errors).  The location is marked
                # `internal` so it is only reachable via nginx's error_page redirect
                # and cannot be requested directly by clients.
                error_page 400 431 500 502 503 504 /gateway_error.html;
                location = /gateway_error.html {{
                    root /usr/local/apisix/error-pages;
                    internal;
                }}
                """
            ),
            "httpAdmin": "",
            "stream": "",
        },
    }


def apisix_helm_values(config: ApisixGatewayConfig) -> dict[str, Any]:
    """Build the values for the APISIX Helm chart.

    :param config: What differs between the environments the gateway runs in.

    :returns: Values for the ``apisix`` chart from ``APISIX_CHART_REPOSITORY``.
    :rtype: dict[str, Any]
    """
    otel_plugin_metadata = (
        _otel_plugin_metadata(config.env_suffix, config.otel_collector_address)
        if config.otel_collector_address is not None
        else {}
    )
    ssl: dict[str, Any] = {
        "enabled": True,
        "containerPort": 9443,
        "sslCiphers": GATEWAY_SSL_CIPHERS,
    }
    if config.trusted_ca is not None:
        # The chart mounts this file and points ssl_trusted_certificate at it.
        ssl["existingCASecret"] = config.trusted_ca.secret_name
        ssl["certCAFilename"] = config.trusted_ca.filename

    return {
        **(
            {
                "image": {
                    "repository": config.image_repository,
                    "pullPolicy": "IfNotPresent",
                }
            }
            if config.image_repository is not None
            else {}
        ),
        **(
            _high_availability_values(config.high_availability)
            if config.high_availability is not None
            else {"autoscaling": {"enabled": False}, "replicaCount": 1}
        ),
        "tolerations": config.tolerations,
        "readinessProbe": {
            # Relaxed to break a chicken-and-egg problem: workers need to be in
            # service to receive config via the Admin API, and report "has not
            # received configuration" until the ingress controller delivers
            # the first sync.
            "initialDelaySeconds": 10,
            "periodSeconds": 10,
            "timeoutSeconds": 1,
            "failureThreshold": 30,  # Allow 5 minutes before marking unhealthy
        },
        # requests.memory != limits.memory deliberately: a transient
        # buffering spike (e.g. several concurrent large course-CSV/
        # JupyterHub-bundle downloads pinned to one pod via HTTP/2)
        # needs burst headroom above the steady-state baseline.  With
        # requests == limits (the prior config), the pod was OOMKilled
        # the instant usage crossed the request value, well before the
        # VPA's periodic recommendation loop could observe the spike
        # and react -- VPA cannot prevent an OOM that happens faster
        # than its sampling/recommendation interval. limits.memory now
        # defaults to double the request so ordinary bursts are
        # absorbed as Burstable QoS headroom instead of an instant
        # hard kill; the VPA (in apisix_official.py) still owns raising
        # the baseline request over time via apisix_memory/apisix_max_memory.
        "resources": {
            "requests": {
                # The CPU request is the HPA's denominator: the
                # autoscaler targets targetCPUUtilizationPercentage of
                # THIS value, so the per-pod scale-out point is
                # target% x request. A request far below real per-pod
                # usage makes maxReplicas reachable at trivial absolute
                # load and turns HPAAtMaxReplicas alerts into noise. Size
                # it per stack (apisix_cpu) from measured per-pod usage,
                # not as a placeholder.
                "cpu": config.cpu_request,
                "memory": config.memory_request,
            },
            "limits": {
                "memory": config.effective_memory_limit,
            },
        },
        **(
            {"podAnnotations": config.pod_annotations}
            if config.pod_annotations is not None
            else {}
        ),
        "service": _gateway_service_values(config),
        # --- APISIX Configuration ---
        "apisix": {
            # Traditional mode with a YAML config provider: the Admin API is
            # backed by YAML storage, and the ingress controller pushes routes
            # through it rather than APISIX reading the YAML directly.
            "deployment": {
                "mode": "traditional",
                "role": "traditional",
                "role_traditional": {
                    "config_provider": "yaml",
                },
            },
            # Enable the opentelemetry plugin globally (it is NOT in the
            # APISIX default plugin list).  Setting ``plugins`` replaces the
            # default list, so APISIX_ENABLED_PLUGINS reproduces the full
            # default set plus opentelemetry.  Omitted where tracing is off
            # so the default list (which already excludes opentelemetry)
            # remains in effect.
            **(
                {"plugins": APISIX_ENABLED_PLUGINS}
                if config.otel_tracing_enabled
                else {}
            ),
            # ``pluginAttrs`` renders to ``plugin_attr`` in config.yaml.
            # NOTE: the APISIX 3.14 opentelemetry plugin does NOT read its
            # collector/resource config from plugin_attr -- it reads it from
            # plugin_metadata (delivered via the GatewayProxy below).  The
            # same config is mirrored here only so older/other plugin builds
            # that still consult plugin_attr keep working; on 3.14 it is
            # inert but harmless.  The plugin only supports binary OTLP over
            # HTTP, so the collector targets the Alloy receiver's HTTP port
            # (4318); the plugin appends ``/v1/traces`` itself.  From the
            # receiver, traces flow through the existing tail-sampling
            # pipeline to Grafana Cloud (see grafana.py gc-otlp-endpoint).
            "pluginAttrs": {
                # Redirect http requests to port 443, not APISIX's
                # internal TLS port 9443 (hq#10999)
                "redirect": {"https_port": DEFAULT_HTTPS_PORT},
                **(
                    {"opentelemetry": otel_plugin_metadata}
                    if config.otel_tracing_enabled
                    else {}
                ),
            },
            **(
                {"trustedAddresses": config.trusted_addresses}
                if config.trusted_addresses is not None
                else {}
            ),
            # Chart default is true, which sends "Server: APISIX/<version>"
            # on every response (hq#14079).
            "enableServerTokens": False,
            # Enable comprehensive per-phase request lifecycle tracing
            # (SSL/SNI, rewrite, access, header_filter, body_filter, log).
            # Available since chart 2.16.0; without it OpenTelemetry only
            # emits a single span per request instead of one per phase.
            "tracing": config.otel_tracing_enabled,
            "admin": {
                "enabled": True,
                "type": "ClusterIP",
                "ip": "0.0.0.0",  # noqa: S104 - ClusterIP service, internal only
                "port": 9180,
                "servicePort": 9180,
                "credentials": {
                    "admin": config.admin_key,
                    "viewer": config.viewer_key,
                },
                "allow": {
                    "ipList": [
                        "0.0.0.0/0",  # Allow all internal access
                    ],
                },
            },
            "ssl": ssl,
            "prometheus": {
                "enabled": True,
                "containerPort": 9091,
                "path": "/apisix/prometheus/metrics",
            },
            "nginx": _nginx_values(config.env_suffix),
        },
        # --- Etcd Configuration ---
        # APISIX runs standalone (role_traditional.config_provider: yaml, set
        # above); the ingress controller pushes config over the Admin API, so
        # there is no etcd StatefulSet in any cluster.
        #
        # Disabling the subchart does NOT stop the chart emitting etcd
        # scaffolding: the rendered config.yaml still carries an `etcd:` block
        # (with upstream's `http://etcd.host:2379` placeholder), the chart still
        # creates the `etcd-apache-apisix` Secret, and the Deployment still gets
        # an APISIX_ETCD_PASSWORD env var bound to it. All three are inert --
        # and all three must stay, since the Deployment's secretKeyRef will fail
        # to start pods without the Secret.
        #
        # Do not read that block as a live endpoint. Doing so once already
        # produced a cleanup ticket built on a false premise; see the
        # "Vestigial etcd Artifacts" section of APISIX_STANDALONE_SETUP.md.
        "etcd": {
            "enabled": False,
        },
        # --- Ingress Controller Configuration ---
        "ingress-controller": {
            "enabled": True,
            "rbac": {
                "create": True,
            },
            "webhook": {"enabled": config.admission_webhook_enabled},
            "serviceAccount": {
                "create": True,
                "name": "apache-apisix-ingress-controller",
            },
            "config": {
                "execADCTimeout": "60s",  # Increase from default 15s for large config syncs
                "provider": {
                    "type": "apisix-standalone",
                },
                "kubernetes": {
                    "ingressClass": APISIX_INGRESS_CLASS,
                    "enableGatewayAPI": True,
                },
            },
            "apisix": {
                "adminService": {
                    "name": "apache-apisix-admin",
                    "namespace": APISIX_NAMESPACE,
                    "port": 9180,
                },
            },
            "gatewayProxy": {
                "createDefault": True,
                "publishService": "apache-apisix-gateway",
                # Global plugin metadata, keyed by plugin name, shared by
                # every instance of that plugin.  The ingress controller
                # syncs this into the data plane's ``plugin_metadata`` via
                # the Admin API.  This is the config surface the APISIX 3.14
                # opentelemetry plugin actually reads (see
                # _otel_plugin_metadata); without it the plugin exports
                # nothing.  Omitted where tracing is off to match the
                # plugin-enablement gate.
                **(
                    {"pluginMetadata": {"opentelemetry": otel_plugin_metadata}}
                    if config.otel_tracing_enabled
                    else {}
                ),
                "provider": {
                    "type": "ControlPlane",
                    "controlPlane": {
                        "service": {
                            "name": "apache-apisix-admin",
                            "port": 9180,
                        },
                        "auth": {
                            "type": "AdminKey",
                            "adminKey": {
                                "value": config.admin_key,
                            },
                        },
                    },
                },
            },
            "deployment": _ingress_controller_deployment_values(config),
            # The ingress-controller sub-chart's built-in PDB is disabled because
            # apisix-ingress-controller v1.1.1 has a template name mismatch bug:
            # pdb.yaml references "apisix-ingress-controller.fullname" but _helpers.tpl
            # only defines "apisix-ingress-controller-manager.*" variants, causing Helm
            # to fail with "no template 'apisix-ingress-controller.fullname'".
            # apisix_official.py creates a standalone PodDisruptionBudget instead.
            "podDisruptionBudget": {
                "enabled": False,
            },
        },
        **(
            {
                "metrics": {
                    "serviceMonitor": {
                        "enabled": True,
                        "namespace": APISIX_NAMESPACE,
                        "labels": config.labels,
                        # Grafana Cloud bills per DPM; the chart's 15s default
                        # is 4x the included rate. Consumers use >=5m windows.
                        "interval": "60s",
                    },
                },
            }
            if config.service_monitor_enabled
            else {}
        ),
        # --- Custom error pages ---
        # Mount the branded gateway error page ConfigMap (gateway_error_pages)
        # into every APISIX pod so nginx can serve it via the error_page
        # directive in configurationSnippet.httpSrv.
        "extraVolumes": [
            {
                "name": "apisix-error-pages",
                "configMap": {"name": GATEWAY_ERROR_PAGES_CONFIGMAP_NAME},
            }
        ],
        "extraVolumeMounts": [
            {
                "name": "apisix-error-pages",
                "mountPath": "/usr/local/apisix/error-pages",
                "readOnly": True,
            }
        ],
    }


def gateway_class_spec() -> dict[str, Any]:
    """Return the spec of the ``apisix`` GatewayClass."""
    return {
        "controllerName": "apisix.apache.org/apisix-ingress-controller",
        "parametersRef": {
            "group": "apisix.apache.org",
            "kind": "GatewayProxy",
            "name": APISIX_GATEWAY_PROXY_NAME,
            "namespace": APISIX_NAMESPACE,
        },
    }


def gateway_spec() -> dict[str, Any]:
    """Return the spec of the ``apisix`` Gateway that HTTPRoutes attach to."""
    return {
        "gatewayClassName": GATEWAY_NAME,
        "listeners": [
            {
                "name": "http",
                "protocol": "HTTP",
                "port": 80,
                "allowedRoutes": {
                    "namespaces": {
                        "from": "All",
                    }
                },
            },
            {
                "name": "https",
                "protocol": "HTTPS",
                "port": 443,
                "allowedRoutes": {
                    "namespaces": {
                        "from": "All",
                    }
                },
                # TLS configuration is intentionally omitted here because it is handled
                # separately by per-application ApisixTls CRDs (see ADR-0003).
            },
        ],
        "infrastructure": {
            "parametersRef": {
                "group": "apisix.apache.org",
                "kind": "GatewayProxy",
                "name": APISIX_GATEWAY_PROXY_NAME,
            }
        },
    }


def identity_header_strip_rule_spec() -> dict[str, Any]:
    """Return the spec of the ``identity-header-strip`` ApisixGlobalRule.

    Everything that has to run on every route ahead of openid-connect: send
    OIDC routes to their canonical https origin, and refuse the gateway's own
    identity headers from a client.  See gateway_global_pre_function_plugin.

    The canonical redirect is here because per-application attachment left
    most OIDC hosts answering plain HTTP with a cleartext session cookie.

    The openid-connect plugin clears an inbound X-Userinfo / X-ID-Token /
    X-Raw-ID-Token / X-Refresh-Token before setting its own, but only where
    it is attached.  Every other route on an OIDC host hands the client's
    copy straight to the application, and mitol-apigateway's middleware
    authenticates off X-Userinfo without asking who wrote it -- so one
    unprotected route on a host that has a login is enough to impersonate
    any account.

    A global rule rather than an entry in each application's shared plugin
    config: the routes at risk are the ones that reference no plugin config
    at all, and a route carrying its own serverless-pre-function (every OIDC
    route does) would override a shared one by plugin name instead of running
    both.  Global rules run after route matching but ahead of the matched
    route's rewrite phase, which is where openid-connect lives, so the strip
    lands before the real headers are set and after nothing.

    The ingress controller flattens every ApisixGlobalRule in the cluster
    into one plugin-name-keyed map (internal/adc/translator/globalrule.go),
    so this is the cluster's only global ``serverless-pre-function``: a
    second one anywhere would silently replace it rather than run alongside.
    Anything else that has to run globally before openid-connect belongs in
    ``gateway_global_pre_function_plugin``'s ``functions`` list.
    """
    return {
        "ingressClassName": APISIX_INGRESS_CLASS,
        "plugins": [
            gateway_global_pre_function_plugin().model_dump(
                by_alias=True, exclude_none=True
            )
        ],
    }
