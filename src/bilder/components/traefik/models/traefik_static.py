# generated by datamodel-codegen:
#   filename:  https://json.schemastore.org/traefik-v2.json
#   timestamp: 2022-10-05T18:42:53+00:00

#   parameters: --snake-case-field --use-subclass-enum --field-include-all-keys --field-constraints --use-schema-description --target-python-version 3.9 --wrap-string-literal --use-default --class-name TraefikStaticConfig --use-standard-collections --enum-field-as-literal=one  # noqa: E501

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field


class FieldModel(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")


class Filters(FieldModel):
    status_codes: Optional[list[str]] = Field(None, alias="statusCodes")
    retry_attempts: Optional[bool] = Field(None, alias="retryAttempts")
    min_duration: Optional[str] = Field(None, alias="minDuration")


class Headers(FieldModel):
    default_mode: Optional[str] = Field(None, alias="defaultMode")
    names: Optional[dict[str, str]] = None


class Fields(FieldModel):
    default_mode: Optional[str] = Field(None, alias="defaultMode")
    names: Optional[dict[str, str]] = None
    headers: Optional[Headers] = None


class AccessLog(FieldModel):
    file_path: Optional[str] = Field(None, alias="filePath")
    format: Optional[str] = None
    filters: Optional[Filters] = None
    fields: Optional[Fields] = Fields(
        default_mode="keep", headers=Headers(default_mode="keep")
    )
    buffering_size: Optional[int] = Field(None, alias="bufferingSize")


class Api(FieldModel):
    insecure: Optional[bool] = None
    dashboard: Optional[bool] = None
    debug: Optional[bool] = None


class Eab(FieldModel):
    kid: Optional[str] = None
    hmac_encoded: Optional[str] = Field(None, alias="hmacEncoded")


class DnsChallenge(FieldModel):
    provider: Optional[str] = None
    delay_before_check: Optional[str] = Field(None, alias="delayBeforeCheck")
    resolvers: Optional[list[str]] = None
    disable_propagation_check: Optional[bool] = Field(
        None, alias="disablePropagationCheck"
    )


class HttpChallenge(FieldModel):
    entry_point: Optional[str] = Field(None, alias="entryPoint")


class Acme(FieldModel):
    email: Optional[str] = None
    ca_server: Optional[str] = Field(None, alias="caServer")
    certificates_duration: Optional[int] = Field(None, alias="certificatesDuration")
    preferred_chain: Optional[str] = Field(None, alias="preferredChain")
    storage: Optional[str] = None
    key_type: Optional[str] = Field(None, alias="keyType")
    eab: Optional[Eab] = None
    dns_challenge: Optional[DnsChallenge] = Field(None, alias="dnsChallenge")
    http_challenge: Optional[HttpChallenge] = Field(None, alias="httpChallenge")
    tls_challenge: Optional[dict[str, Any]] = Field(None, alias="tlsChallenge")


class CertificatesResolvers(FieldModel):
    acme: Optional[Acme] = None


class LifeCycle(FieldModel):
    request_accept_grace_timeout: Optional[str] = Field(
        None, alias="requestAcceptGraceTimeout"
    )
    grace_time_out: Optional[str] = Field(None, alias="graceTimeOut")


class RespondingTimeouts(FieldModel):
    read_timeout: Optional[str] = Field(None, alias="readTimeout")
    write_timeout: Optional[str] = Field(None, alias="writeTimeout")
    idle_timeout: Optional[str] = Field(None, alias="idleTimeout")


class Transport(FieldModel):
    life_cycle: Optional[LifeCycle] = Field(None, alias="lifeCycle")
    responding_timeouts: Optional[RespondingTimeouts] = Field(
        None, alias="respondingTimeouts"
    )


class ProxyProtocol(FieldModel):
    insecure: Optional[bool] = None
    trusted_i_ps: Optional[list[str]] = Field(None, alias="trustedIPs")


class ForwardedHeaders(FieldModel):
    insecure: Optional[bool] = None
    trusted_ips: Optional[list[str]] = Field(None, alias="trustedIPs")


class EntryPoint(FieldModel):
    to: Optional[str] = None
    scheme: Optional[str] = None
    permanent: Optional[bool] = None
    priority: Optional[int] = None


class Redirections(FieldModel):
    entry_point: Optional[EntryPoint] = Field(None, alias="entryPoint")


class Domain(FieldModel):
    main: Optional[str] = None
    sans: Optional[list[str]] = None


class Tls(FieldModel):
    options: Optional[str] = None
    cert_resolver: Optional[str] = Field(None, alias="certResolver")
    domains: Optional[list[Domain]] = None


class Http(FieldModel):
    redirections: Optional[Redirections] = None
    middlewares: Optional[list[str]] = None
    tls: Optional[Tls] = None


class Http2(FieldModel):
    max_concurrent_streams: Optional[int] = Field(None, alias="maxConcurrentStreams")


class Http3(FieldModel):
    advertised_port: Optional[int] = Field(None, alias="advertisedPort")


class Udp(FieldModel):
    timeout: Optional[str] = None


class EntryPoints(FieldModel):
    address: Optional[str] = None
    transport: Optional[Transport] = None
    proxy_protocol: Optional[ProxyProtocol] = Field(None, alias="proxyProtocol")
    forwarded_headers: Optional[ForwardedHeaders] = Field(
        ForwardedHeaders(
            trusted_ips=[
                "10.0.0.0/8",
                "172.16.0.0/12",
                "192.168.0.0/16",
            ]
        ),
        alias="forwardedHeaders",
    )
    http: Optional[Http] = None
    http2: Optional[Http2] = None
    http3: Optional[Http3] = None
    udp: Optional[Udp] = None


class Plugins(FieldModel):
    module_name: Optional[str] = Field(None, alias="moduleName")
    version: Optional[str] = None


class LocalPlugins(FieldModel):
    module_name: Optional[str] = Field(None, alias="moduleName")


class Experimental(FieldModel):
    kubernetes_gateway: Optional[bool] = Field(None, alias="kubernetesGateway")
    http3: Optional[bool] = None
    hub: Optional[bool] = None
    plugins: Optional[dict[str, Plugins]] = None
    local_plugins: Optional[dict[str, LocalPlugins]] = Field(None, alias="localPlugins")


class Global(FieldModel):
    check_new_version: Optional[bool] = Field(None, alias="checkNewVersion")
    send_anonymous_usage: Optional[bool] = Field(None, alias="sendAnonymousUsage")


class HostResolver(FieldModel):
    cname_flattening: Optional[bool] = Field(None, alias="cnameFlattening")
    resolv_config: Optional[str] = Field(None, alias="resolvConfig")
    resolv_depth: Optional[int] = Field(None, alias="resolvDepth")


class Tls1(FieldModel):
    insecure: Optional[bool] = None
    ca: Optional[str] = None
    cert: Optional[str] = None
    key: Optional[str] = None


class Hub(FieldModel):
    tls: Optional[Tls1] = None


class Log(FieldModel):
    level: Optional[str] = None
    file_path: Optional[str] = Field(None, alias="filePath")
    format: Optional[str] = None


class Prometheus(FieldModel):
    buckets: Optional[list[float]] = None
    add_entry_points_labels: Optional[bool] = Field(None, alias="addEntryPointsLabels")
    add_routers_labels: Optional[bool] = Field(None, alias="addRoutersLabels")
    add_services_labels: Optional[bool] = Field(None, alias="addServicesLabels")
    entry_point: Optional[str] = Field(None, alias="entryPoint")
    manual_routing: Optional[bool] = Field(None, alias="manualRouting")


class Datadog(FieldModel):
    address: Optional[str] = None
    push_interval: Optional[str] = Field(None, alias="pushInterval")
    add_entry_points_labels: Optional[bool] = Field(None, alias="addEntryPointsLabels")
    add_routers_labels: Optional[bool] = Field(None, alias="addRoutersLabels")
    add_services_labels: Optional[bool] = Field(None, alias="addServicesLabels")
    prefix: Optional[str] = None


class StatsD(FieldModel):
    address: Optional[str] = None
    push_interval: Optional[str] = Field(None, alias="pushInterval")
    add_entry_points_labels: Optional[bool] = Field(None, alias="addEntryPointsLabels")
    add_routers_labels: Optional[bool] = Field(None, alias="addRoutersLabels")
    add_services_labels: Optional[bool] = Field(None, alias="addServicesLabels")
    prefix: Optional[str] = None


class InfluxDb(FieldModel):
    address: Optional[str] = None
    protocol: Optional[str] = None
    push_interval: Optional[str] = Field(None, alias="pushInterval")
    database: Optional[str] = None
    retention_policy: Optional[str] = Field(None, alias="retentionPolicy")
    username: Optional[str] = None
    password: Optional[str] = None
    add_entry_points_labels: Optional[bool] = Field(None, alias="addEntryPointsLabels")
    add_routers_labels: Optional[bool] = Field(None, alias="addRoutersLabels")
    add_services_labels: Optional[bool] = Field(None, alias="addServicesLabels")
    additional_labels: Optional[dict[str, Any]] = Field(None, alias="additionalLabels")


class InfluxDb2(FieldModel):
    address: Optional[str] = None
    token: Optional[str] = None
    push_interval: Optional[str] = Field(None, alias="pushInterval")
    org: Optional[str] = None
    bucket: Optional[str] = None
    add_entry_points_labels: Optional[bool] = Field(None, alias="addEntryPointsLabels")
    add_routers_labels: Optional[bool] = Field(None, alias="addRoutersLabels")
    add_services_labels: Optional[bool] = Field(None, alias="addServicesLabels")
    additional_labels: Optional[dict[str, Any]] = Field(None, alias="additionalLabels")


class Metrics(FieldModel):
    prometheus: Optional[Prometheus] = None
    datadog: Optional[Datadog] = None
    stats_d: Optional[StatsD] = Field(None, alias="statsD")
    influx_db: Optional[InfluxDb] = Field(None, alias="influxDB")
    influx_db2: Optional[InfluxDb2] = Field(None, alias="influxDB2")


class Pilot(FieldModel):
    token: Optional[str] = None
    dashboard: Optional[bool] = None


class Ping(FieldModel):
    entry_point: Optional[str] = Field(None, alias="entryPoint")
    manual_routing: Optional[bool] = Field(None, alias="manualRouting")
    terminating_status_code: Optional[int] = Field(None, alias="terminatingStatusCode")


class Tls2(FieldModel):
    ca: Optional[str] = None
    ca_optional: Optional[bool] = Field(None, alias="caOptional")
    cert: Optional[str] = None
    key: Optional[str] = None
    insecure_skip_verify: Optional[bool] = Field(None, alias="insecureSkipVerify")


class Docker(FieldModel):
    allow_empty_services: Optional[bool] = Field(None, alias="allowEmptyServices")
    constraints: Optional[str] = None
    default_rule: Optional[str] = Field(None, alias="defaultRule")
    endpoint: Optional[str] = None
    exposed_by_default: Optional[bool] = Field(None, alias="exposedByDefault")
    http_client_timeout: Optional[str] = Field(None, alias="httpClientTimeout")
    network: Optional[str] = None
    tls: Optional[Tls2] = None
    use_bind_port_ip: Optional[bool] = Field(None, alias="useBindPortIP")
    watch: Optional[bool] = None


class Swarm(FieldModel):
    allow_empty_services: Optional[bool] = Field(None, alias="allowEmptyServices")
    constraints: Optional[str] = None
    default_rule: Optional[str] = Field(None, alias="defaultRule")
    endpoint: Optional[str] = None
    exposed_by_default: Optional[bool] = Field(None, alias="exposedByDefault")
    http_client_timeout: Optional[str] = Field(None, alias="httpClientTimeout")
    network: Optional[str] = None
    refresh_seconds: Optional[str] = Field(None, alias="refreshSeconds")
    tls: Optional[Tls2] = None
    use_bind_port_ip: Optional[bool] = Field(None, alias="useBindPortIP")
    watch: Optional[bool] = None


class FileProvider(FieldModel):
    directory: Optional[str] = None
    watch: Optional[bool] = None
    filename: Optional[str] = None
    debug_log_generated_template: Optional[bool] = Field(
        None, alias="debugLogGeneratedTemplate"
    )


class Tls3(FieldModel):
    ca: Optional[str] = None
    ca_optional: Optional[bool] = Field(None, alias="caOptional")
    cert: Optional[str] = None
    key: Optional[str] = None
    insecure_skip_verify: Optional[bool] = Field(None, alias="insecureSkipVerify")


class Basic(FieldModel):
    http_basic_auth_user: Optional[str] = Field(None, alias="httpBasicAuthUser")
    http_basic_password: Optional[str] = Field(None, alias="httpBasicPassword")


class Marathon(FieldModel):
    constraints: Optional[str] = None
    trace: Optional[bool] = None
    watch: Optional[bool] = None
    endpoint: Optional[str] = None
    default_rule: Optional[str] = Field(None, alias="defaultRule")
    exposed_by_default: Optional[bool] = Field(None, alias="exposedByDefault")
    dcos_token: Optional[str] = Field(None, alias="dcosToken")
    tls: Optional[Tls3] = None
    dialer_timeout: Optional[str] = Field(None, alias="dialerTimeout")
    response_header_timeout: Optional[str] = Field(None, alias="responseHeaderTimeout")
    tls_handshake_timeout: Optional[str] = Field(None, alias="tlsHandshakeTimeout")
    keep_alive: Optional[str] = Field(None, alias="keepAlive")
    force_task_hostname: Optional[bool] = Field(None, alias="forceTaskHostname")
    basic: Optional[Basic] = None
    respect_readiness_checks: Optional[bool] = Field(
        None, alias="respectReadinessChecks"
    )


class IngressEndpoint(FieldModel):
    ip: Optional[str] = None
    hostname: Optional[str] = None
    published_service: Optional[str] = Field(None, alias="publishedService")


class KubernetesIngress(FieldModel):
    endpoint: Optional[str] = None
    token: Optional[str] = None
    cert_auth_file_path: Optional[str] = Field(None, alias="certAuthFilePath")
    namespaces: Optional[list[str]] = None
    label_selector: Optional[str] = Field(None, alias="labelSelector")
    ingress_class: Optional[str] = Field(None, alias="ingressClass")
    throttle_duration: Optional[str] = Field(None, alias="throttleDuration")
    allow_empty_services: Optional[bool] = Field(None, alias="allowEmptyServices")
    allow_external_name_services: Optional[bool] = Field(
        None, alias="allowExternalNameServices"
    )
    ingress_endpoint: Optional[IngressEndpoint] = Field(None, alias="ingressEndpoint")


class KubernetesCrd(FieldModel):
    endpoint: Optional[str] = None
    token: Optional[str] = None
    cert_auth_file_path: Optional[str] = Field(None, alias="certAuthFilePath")
    namespaces: Optional[list[str]] = None
    allow_cross_namespace: Optional[bool] = Field(None, alias="allowCrossNamespace")
    allow_external_name_services: Optional[bool] = Field(
        None, alias="allowExternalNameServices"
    )
    label_selector: Optional[str] = Field(None, alias="labelSelector")
    ingress_class: Optional[str] = Field(None, alias="ingressClass")
    throttle_duration: Optional[str] = Field(None, alias="throttleDuration")
    allow_empty_services: Optional[bool] = Field(None, alias="allowEmptyServices")


class KubernetesGateway(FieldModel):
    endpoint: Optional[str] = None
    token: Optional[str] = None
    cert_auth_file_path: Optional[str] = Field(None, alias="certAuthFilePath")
    namespaces: Optional[list[str]] = None
    label_selector: Optional[str] = Field(None, alias="labelSelector")
    throttle_duration: Optional[str] = Field(None, alias="throttleDuration")


class Rest(FieldModel):
    insecure: Optional[bool] = None


class Rancher(FieldModel):
    constraints: Optional[str] = None
    watch: Optional[bool] = None
    default_rule: Optional[str] = Field(None, alias="defaultRule")
    exposed_by_default: Optional[bool] = Field(None, alias="exposedByDefault")
    enable_service_health_filter: Optional[bool] = Field(
        None, alias="enableServiceHealthFilter"
    )
    refresh_seconds: Optional[int] = Field(None, alias="refreshSeconds")
    interval_poll: Optional[bool] = Field(None, alias="intervalPoll")
    prefix: Optional[str] = None


class Tls4(FieldModel):
    ca: Optional[str] = None
    ca_optional: Optional[bool] = Field(None, alias="caOptional")
    cert: Optional[str] = None
    key: Optional[str] = None
    insecure_skip_verify: Optional[bool] = Field(None, alias="insecureSkipVerify")


class HttpAuth(FieldModel):
    username: Optional[str] = None
    password: Optional[str] = None


class Endpoint(FieldModel):
    address: Optional[str] = None
    scheme: Optional[str] = None
    datacenter: Optional[str] = None
    token: Optional[str] = None
    endpoint_wait_time: Optional[str] = Field(None, alias="endpointWaitTime")
    tls: Optional[Tls4] = None
    http_auth: Optional[HttpAuth] = Field(None, alias="httpAuth")


class ConsulCatalog(FieldModel):
    constraints: Optional[str] = None
    prefix: Optional[str] = None
    refresh_interval: Optional[str] = Field(None, alias="refreshInterval")
    require_consistent: Optional[bool] = Field(None, alias="requireConsistent")
    stale: Optional[bool] = None
    cache: Optional[bool] = None
    exposed_by_default: Optional[bool] = Field(None, alias="exposedByDefault")
    default_rule: Optional[str] = Field(None, alias="defaultRule")
    connect_aware: Optional[bool] = Field(None, alias="connectAware")
    connect_by_default: Optional[bool] = Field(None, alias="connectByDefault")
    service_name: Optional[str] = Field(None, alias="serviceName")
    namespace: Optional[str] = None
    namespaces: Optional[list[str]] = None
    watch: Optional[bool] = None
    endpoint: Optional[Endpoint] = None


class Tls5(FieldModel):
    ca: Optional[str] = None
    ca_optional: Optional[bool] = Field(None, alias="caOptional")
    cert: Optional[str] = None
    key: Optional[str] = None
    insecure_skip_verify: Optional[bool] = Field(None, alias="insecureSkipVerify")


class Endpoint1(FieldModel):
    address: Optional[str] = None
    region: Optional[str] = None
    token: Optional[str] = None
    endpoint_wait_time: Optional[str] = Field(None, alias="endpointWaitTime")
    tls: Optional[Tls5] = None


class Nomad(FieldModel):
    constraints: Optional[str] = None
    prefix: Optional[str] = None
    refresh_interval: Optional[str] = Field(None, alias="refreshInterval")
    stale: Optional[bool] = None
    exposed_by_default: Optional[bool] = Field(None, alias="exposedByDefault")
    default_rule: Optional[str] = Field(None, alias="defaultRule")
    namespace: Optional[str] = None
    endpoint: Optional[Endpoint1] = None


class Ecs(FieldModel):
    constraints: Optional[str] = None
    exposed_by_default: Optional[bool] = Field(None, alias="exposedByDefault")
    ecs_anywhere: Optional[bool] = Field(None, alias="ecsAnywhere")
    refresh_seconds: Optional[int] = Field(None, alias="refreshSeconds")
    default_rule: Optional[str] = Field(None, alias="defaultRule")
    clusters: Optional[list[str]] = None
    auto_discover_clusters: Optional[bool] = Field(None, alias="autoDiscoverClusters")
    region: Optional[str] = None
    access_key_id: Optional[str] = Field(None, alias="accessKeyID")
    secret_access_key: Optional[str] = Field(None, alias="secretAccessKey")


class Tls6(FieldModel):
    ca: Optional[str] = None
    ca_optional: Optional[bool] = Field(None, alias="caOptional")
    cert: Optional[str] = None
    key: Optional[str] = None
    insecure_skip_verify: Optional[bool] = Field(None, alias="insecureSkipVerify")


class Consul(FieldModel):
    root_key: Optional[str] = Field(None, alias="rootKey")
    endpoints: Optional[list[str]] = None
    token: Optional[str] = None
    namespace: Optional[str] = None
    namespaces: Optional[list[str]] = None
    tls: Optional[Tls6] = None


class Tls7(FieldModel):
    ca: Optional[str] = None
    ca_optional: Optional[bool] = Field(None, alias="caOptional")
    cert: Optional[str] = None
    key: Optional[str] = None
    insecure_skip_verify: Optional[bool] = Field(None, alias="insecureSkipVerify")


class Etcd(FieldModel):
    root_key: Optional[str] = Field(None, alias="rootKey")
    endpoints: Optional[list[str]] = None
    username: Optional[str] = None
    password: Optional[str] = None
    tls: Optional[Tls7] = None


class ZooKeeper(FieldModel):
    root_key: Optional[str] = Field(None, alias="rootKey")
    endpoints: Optional[list[str]] = None
    username: Optional[str] = None
    password: Optional[str] = None


class Tls8(FieldModel):
    ca: Optional[str] = None
    ca_optional: Optional[bool] = Field(None, alias="caOptional")
    cert: Optional[str] = None
    key: Optional[str] = None
    insecure_skip_verify: Optional[bool] = Field(None, alias="insecureSkipVerify")


class Redis(FieldModel):
    root_key: Optional[str] = Field(None, alias="rootKey")
    endpoints: Optional[list[str]] = None
    username: Optional[str] = None
    password: Optional[str] = None
    db: Optional[int] = None
    tls: Optional[Tls8] = None


class Tls9(FieldModel):
    ca: Optional[str] = None
    ca_optional: Optional[bool] = Field(None, alias="caOptional")
    cert: Optional[str] = None
    key: Optional[str] = None
    insecure_skip_verify: Optional[bool] = Field(None, alias="insecureSkipVerify")


class Http1(FieldModel):
    endpoint: Optional[str] = None
    poll_interval: Optional[str] = Field(None, alias="pollInterval")
    poll_timeout: Optional[str] = Field(None, alias="pollTimeout")
    tls: Optional[Tls9] = None


class Providers(FieldModel):
    providers_throttle_duration: Optional[str] = Field(
        None, alias="providersThrottleDuration"
    )
    docker: Optional[Docker] = None
    file: Optional[FileProvider] = None
    marathon: Optional[Marathon] = None
    kubernetes_ingress: Optional[KubernetesIngress] = Field(
        None, alias="kubernetesIngress"
    )
    kubernetes_crd: Optional[KubernetesCrd] = Field(None, alias="kubernetesCRD")
    kubernetes_gateway: Optional[KubernetesGateway] = Field(
        None, alias="kubernetesGateway"
    )
    rest: Optional[Rest] = None
    rancher: Optional[Rancher] = None
    consul_catalog: Optional[ConsulCatalog] = Field(None, alias="consulCatalog")
    nomad: Optional[Nomad] = None
    ecs: Optional[Ecs] = None
    consul: Optional[Consul] = None
    etcd: Optional[Etcd] = None
    zoo_keeper: Optional[ZooKeeper] = Field(None, alias="zooKeeper")
    redis: Optional[Redis] = None
    http: Optional[Http1] = None
    plugin: Optional[dict[str, dict[str, Any]]] = None


class ForwardingTimeouts(FieldModel):
    dial_timeout: Optional[str] = Field(None, alias="dialTimeout")
    response_header_timeout: Optional[str] = Field(None, alias="responseHeaderTimeout")
    idle_conn_timeout: Optional[str] = Field(None, alias="idleConnTimeout")


class ServersTransport(FieldModel):
    insecure_skip_verify: Optional[bool] = Field(None, alias="insecureSkipVerify")
    root_c_as: Optional[list[str]] = Field(None, alias="rootCAs")
    max_idle_conns_per_host: Optional[int] = Field(None, alias="maxIdleConnsPerHost")
    forwarding_timeouts: Optional[ForwardingTimeouts] = Field(
        None, alias="forwardingTimeouts"
    )


class Collector(FieldModel):
    endpoint: Optional[str] = None
    user: Optional[str] = None
    password: Optional[str] = None


class Jaeger(FieldModel):
    sampling_server_url: Optional[str] = Field(None, alias="samplingServerURL")
    sampling_type: Optional[str] = Field(None, alias="samplingType")
    sampling_param: Optional[int] = Field(None, alias="samplingParam")
    local_agent_host_port: Optional[str] = Field(None, alias="localAgentHostPort")
    gen128_bit: Optional[bool] = Field(None, alias="gen128Bit")
    propagation: Optional[str] = None
    trace_context_header_name: Optional[str] = Field(
        None, alias="traceContextHeaderName"
    )
    disable_attempt_reconnecting: Optional[bool] = Field(
        None, alias="disableAttemptReconnecting"
    )
    collector: Optional[Collector] = None


class Zipkin(FieldModel):
    http_endpoint: Optional[str] = Field(None, alias="httpEndpoint")
    same_span: Optional[bool] = Field(None, alias="sameSpan")
    id128_bit: Optional[bool] = Field(None, alias="id128Bit")
    sample_rate: Optional[int] = Field(None, alias="sampleRate")


class Datadog1(FieldModel):
    local_agent_host_port: Optional[str] = Field(None, alias="localAgentHostPort")
    global_tag: Optional[str] = Field(None, alias="globalTag")
    global_tags: Optional[dict[str, str]] = Field(
        None,
        alias="globalTags",
        description="Sets a list of key:value tags on all spans.",
    )
    debug: Optional[bool] = None
    priority_sampling: Optional[bool] = Field(None, alias="prioritySampling")
    trace_id_header_name: Optional[str] = Field(None, alias="traceIDHeaderName")
    parent_id_header_name: Optional[str] = Field(None, alias="parentIDHeaderName")
    sampling_priority_header_name: Optional[str] = Field(
        None, alias="samplingPriorityHeaderName"
    )
    bagage_prefix_header_name: Optional[str] = Field(
        None, alias="bagagePrefixHeaderName"
    )


class Instana(FieldModel):
    local_agent_host: Optional[str] = Field(None, alias="localAgentHost")
    local_agent_port: Optional[int] = Field(None, alias="localAgentPort")
    log_level: Optional[str] = Field(None, alias="logLevel")
    enable_auto_profile: Optional[bool] = Field(None, alias="enableAutoProfile")


class Haystack(FieldModel):
    local_agent_host: Optional[str] = Field(None, alias="localAgentHost")
    local_agent_port: Optional[int] = Field(None, alias="localAgentPort")
    global_tag: Optional[str] = Field(None, alias="globalTag")
    trace_id_header_name: Optional[str] = Field(None, alias="traceIDHeaderName")
    parent_id_header_name: Optional[str] = Field(None, alias="parentIDHeaderName")
    span_id_header_name: Optional[str] = Field(None, alias="spanIDHeaderName")
    baggage_prefix_header_name: Optional[str] = Field(
        None, alias="baggagePrefixHeaderName"
    )


class Elastic(FieldModel):
    server_url: Optional[str] = Field(None, alias="serverURL")
    secret_token: Optional[str] = Field(None, alias="secretToken")
    service_environment: Optional[str] = Field(None, alias="serviceEnvironment")


class Tracing(FieldModel):
    service_name: Optional[str] = Field(None, alias="serviceName")
    span_name_limit: Optional[int] = Field(None, alias="spanNameLimit")
    jaeger: Optional[Jaeger] = None
    zipkin: Optional[Zipkin] = None
    datadog: Optional[Datadog1] = None
    instana: Optional[Instana] = None
    haystack: Optional[Haystack] = None
    elastic: Optional[Elastic] = None


class TraefikStaticConfig(FieldModel):
    access_log: Optional[AccessLog] = Field(AccessLog(), alias="accessLog")
    api: Optional[Api] = None
    certificates_resolvers: Optional[dict[str, CertificatesResolvers]] = Field(
        None, alias="certificatesResolvers"
    )
    entry_points: Optional[dict[str, EntryPoints]] = Field(None, alias="entryPoints")
    experimental: Optional[Experimental] = None
    global_: Optional[Global] = Field(None, alias="global")
    host_resolver: Optional[HostResolver] = Field(None, alias="hostResolver")
    hub: Optional[Hub] = None
    log: Optional[Log] = None
    metrics: Optional[Metrics] = None
    pilot: Optional[Pilot] = None
    ping: Optional[Ping] = None
    providers: Optional[Providers] = None
    servers_transport: Optional[ServersTransport] = Field(
        None, alias="serversTransport"
    )
    tracing: Optional[Tracing] = None
