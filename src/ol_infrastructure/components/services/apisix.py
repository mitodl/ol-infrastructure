# ruff: noqa: E501
"""APISIX ingress controller components for Kubernetes."""

from typing import Any, Literal

import pulumi_kubernetes as kubernetes
from pulumi import ComponentResource, Output, ResourceOptions
from pydantic import BaseModel, Field, NonNegativeInt, field_validator, model_validator

from bridge.lib.constants import DEFAULT_OIDC_SESSION_COOKIE_NAME
from ol_infrastructure.components.services.vault import (
    OLVaultK8SSecret,
    OLVaultK8SStaticSecretConfig,
)
from ol_infrastructure.lib.pulumi_helper import parse_stack


class OLApisixPluginConfig(BaseModel):
    """Configuration for a single APISIX plugin instance.

    Used with both legacy ApisixRoute/ApisixPluginConfig (v2) and Gateway API
    HTTPRoute/PluginConfig (v1alpha1).  When targeting v1alpha1, the ``enable``
    and ``secretRef`` fields are ignored — see ``OLApisixHTTPRoute`` for details.
    """

    name: str
    enable: bool = True
    secret_ref: str | None = Field(
        default=None,
        alias="secretRef",
    )
    config: dict[str, Any] = {}


def stale_session_cookie_cleanup_plugin(
    cookie_domains: list[str] | None = None,
    stale_cookie_name: str = DEFAULT_OIDC_SESSION_COOKIE_NAME,
) -> OLApisixPluginConfig:
    """Build a plugin that evicts a session cookie an application no longer uses.

    Renaming an OIDC session cookie does not remove the old one: the browser
    keeps sending the abandoned cookie to every route on the host, where the
    plugin now ignores it.  lua-resty-session cookies are large -- large enough
    that we watch their size on a dashboard -- and they are session-scoped, so
    they only expire when the user fully quits the browser, which most people
    never do.  This emits the matching ``Set-Cookie ... Max-Age=0`` so the
    browser drops them on the next response instead.

    The deletion is conditional: it only fires when a stale cookie is actually
    present on the request, so it costs nothing on the overwhelming majority of
    responses and stops firing for a given browser after the one response that
    clears it.  Chunked cookies are handled too -- lua-resty-session splits an
    oversized payload across ``<name>``, ``<name>_2``, ``<name>_3``... and each
    chunk is its own cookie that has to be expired individually.

    A cookie's identity is (name, domain, path), so the domain-scoped and
    host-only variants are separate entries in the browser's jar and each needs
    its own deletion.  ``cookie_domains`` must therefore list exactly the parent
    domains the stale cookie was ever scoped to, and the caller has to be able
    to name them: attaching this to routes on a host that is not under one of
    the listed domains just gets the header rejected by the browser, and
    deriving a domain from the request host risks clearing an unrelated
    ``.mit.edu`` cookie belonging to someone else.  The host-only variant is
    always cleared.

    :param cookie_domains: Parent domains (leading dot, e.g. ``.learn.mit.edu``)
        the stale cookie was scoped to, in addition to the host-only variant.
    :param stale_cookie_name: Name of the abandoned cookie.  Defaults to
        lua-resty-session's built-in name, which is what every application that
        has adopted an explicit name was previously using.

    :returns: A ``serverless-post-function`` plugin config to attach to routes.
    :rtype: OLApisixPluginConfig
    """
    scopes = ["", *[f"; Domain={domain}" for domain in cookie_domains or []]]
    expirations = "\n".join(
        f'            core.response.add_header("Set-Cookie", name .. "=; '
        f'Path=/; Max-Age=0; Secure; HttpOnly; SameSite=Lax{scope}")'
        for scope in scopes
    )
    cleanup_function = f"""return function(conf, ctx)
    local cookie = ngx.var.http_cookie
    if not cookie then
        return
    end
    local core = require("apisix.core")
    for pair in cookie:gmatch("[^;]+") do
        local name = pair:match("^%s*([^=%s]+)=")
        if name and (name == "{stale_cookie_name}"
                     or name:match("^{stale_cookie_name}_%d+$")) then
{expirations}
        end
    end
end"""
    return OLApisixPluginConfig(
        name="serverless-post-function",
        secretRef=None,
        # header_filter rather than the plugin's default log phase: the response
        # headers are still mutable there, and this needs to run after the
        # openid-connect plugin has set the cookies it *does* own so that
        # add_header appends to them rather than being overwritten.
        config={"phase": "header_filter", "functions": [cleanup_function]},
    )


class OLApisixRouteConfig(BaseModel):
    """Configuration for a single ApisixRoute rule (legacy CRD path)."""

    route_name: str
    priority: int = 0
    shared_plugin_config_name: str | None = None
    plugins: list[Any] = []
    hosts: list[str] = []
    paths: list[str] = []
    # Optional ApisixRoute ``match.exprs`` entries for matching on headers,
    # query args, cookies, etc. in addition to host/path. Each entry follows the
    # ApisixRoute v2 schema, e.g.
    # ``{"subject": {"scope": "Header", "name": "Authorization"},
    #    "op": "RegexMatch", "value": ".+"}``.
    # Ref: https://apisix.apache.org/docs/ingress-controller/concepts/apisix_route/#advanced-route-matching
    exprs: list[dict[str, Any]] | None = None
    backend_service_name: str | None = None
    backend_service_port: str | NonNegativeInt | None = None
    # Ref: https://apisix.apache.org/docs/ingress-controller/concepts/apisix_route/#service-resolution-granularity
    backend_resolve_granularity: Literal["endpoint", "service"] = "service"
    upstream: str | None = None
    websocket: bool = False
    timeout_connect: str = "60s"
    timeout_read: str = "60s"
    timeout_send: str = "60s"

    @field_validator("timeout_connect", "timeout_read", "timeout_send")
    @classmethod
    def validate_timeout(cls, v: str) -> str:
        """Ensure that the timeout value is a non-negative integer followed by 's'."""
        if not v.endswith("s") or not v[:-1].isdigit() or int(v[:-1]) <= 0:
            msg = "Timeout must be a positive integer greater than 0 followed by 's' (e.g. '60s')"
            raise ValueError(msg)
        return v

    @field_validator("plugins")
    @classmethod
    def ensure_request_id_plugin(cls, v: list[Any]) -> list[Any]:
        """
        Ensure that the request-id plugin is always added to the plugins list
        """
        if not any(
            (plugin.get("name") if isinstance(plugin, dict) else plugin.name)
            == "request-id"
            for plugin in v
        ):
            v.append(
                OLApisixPluginConfig(
                    name="request-id",
                    secretRef=None,
                    config={"include_in_response": True},
                )
            )
        return v

    @model_validator(mode="after")
    def check_backend_or_upstream(self) -> "OLApisixRouteConfig":
        """Ensure that either upstream or backend service details are provided, not both."""
        upstream: str | None = self.upstream
        backend_service_name: str | None = self.backend_service_name
        backend_service_port: str | NonNegativeInt | None = self.backend_service_port

        if upstream is not None:
            if backend_service_name is not None or backend_service_port is not None:
                msg = "If 'upstream' is provided, 'backend_service_name' and 'backend_service_port' must not be provided."
                raise ValueError(msg)
        elif backend_service_name is None or backend_service_port is None:
            msg = "If 'upstream' is not provided, both 'backend_service_name' and 'backend_service_port' must be provided."
            raise ValueError(msg)
        return self


class OLApisixRoute(ComponentResource):
    """
    Route configuration for apisix
    Defines and creates an "ApisixRoute" resource in the k8s cluster
    """

    def __init__(  # noqa: PLR0913
        self,
        name: str,
        route_configs: list[OLApisixRouteConfig],
        k8s_namespace: str,
        k8s_labels: dict[str, str],
        ingress_class_name: str = "apache-apisix",
        opts: ResourceOptions | None = None,
    ):
        """Initialize the OLApisixRoute component resource."""
        super().__init__(
            "ol:infrastructure:services:k8s:OLApisixRoute", name, None, opts
        )

        resource_options = ResourceOptions(parent=self).merge(opts)

        self.apisix_route_resource = kubernetes.apiextensions.CustomResource(
            f"OLApisixRoute-{name}",
            api_version="apisix.apache.org/v2",
            kind="ApisixRoute",
            metadata=kubernetes.meta.v1.ObjectMetaArgs(
                name=name,
                labels=k8s_labels,
                namespace=k8s_namespace,
            ),
            spec={
                "ingressClassName": ingress_class_name,
                "http": self.__build_route_list(route_configs),
            },
            opts=resource_options.merge(ResourceOptions(delete_before_replace=True)),
        )

    @classmethod
    def __build_route_list(
        cls, route_configs: list[OLApisixRouteConfig]
    ) -> list[dict[str, Any]]:
        routes = []
        for route_config in route_configs:
            route: dict[str, Any] = {
                "name": route_config.route_name,
                "priority": route_config.priority,
                "plugins": [
                    p
                    if isinstance(p, dict)
                    else p.model_dump(by_alias=True, exclude_none=True)
                    for p in route_config.plugins
                ],
                "match": {
                    "hosts": route_config.hosts,
                    "paths": route_config.paths,
                    **({"exprs": route_config.exprs} if route_config.exprs else {}),
                },
                "websocket": route_config.websocket,
                "timeout": {
                    "connect": route_config.timeout_connect,
                    "send": route_config.timeout_send,
                    "read": route_config.timeout_read,
                },
            }
            if route_config.shared_plugin_config_name is not None:
                route["plugin_config_name"] = route_config.shared_plugin_config_name
            if route_config.upstream:
                route["upstreams"] = [{"name": route_config.upstream}]
            else:
                route["backends"] = [
                    {
                        "serviceName": route_config.backend_service_name,
                        "servicePort": route_config.backend_service_port,
                        "resolveGranularity": route_config.backend_resolve_granularity,
                    }
                ]
            routes.append(route)
        return routes


class OLApisixOIDCConfig(BaseModel):
    """Configuration for APISIX OIDC authentication resources.

    Holds Vault path details and OIDC plugin settings used to create the
    per-application Kubernetes Secret (via OLVaultK8SSecret) and generate
    the openid-connect plugin configuration block.
    """

    application_name: str
    k8s_labels: dict[str, str] = {}
    k8s_namespace: str
    vault_mount: str = "secret-operations"
    vault_mount_type: Literal["kv-v1", "kv-v2"] = "kv-v1"
    vault_path: str
    vaultauth: str

    oidc_bearer_only: bool = False
    oidc_introspection_endpoint_auth_method: str = "client_secret_basic"
    oidc_logout_path: str = "/logout/oidc"
    oidc_post_logout_redirect_uri: str = "/"
    oidc_renew_access_token_on_expiry: bool = True
    oidc_scope: str = "openid profile email organization:*"
    oidc_session_contents: dict[str, bool] = {
        "access_token": True,
        "enc_id_token": True,
        "id_token": True,
        "user": True,
    }
    oidc_session_cookie_domain: str | None = None
    # None leaves lua-resty-session's default cookie name ("session") in
    # place. Applications that share a parent domain with other environments
    # or other applications should set an explicit, environment-scoped name so
    # their session cookies stay distinct in the browser's cookie jar.
    oidc_session_cookie_name: str | None = None
    oidc_session_absolute_timeout: NonNegativeInt = 0
    # None leaves lua-resty-session's compiled-in defaults (900s idling /
    # 3600s rolling) untouched for callers that haven't opted in. 0
    # explicitly disables that check, deferring to absolute_timeout (see
    # oidc_session_absolute_timeout) and the upstream Keycloak SSO session.
    oidc_session_idling_timeout: NonNegativeInt | None = None
    oidc_session_rolling_timeout: NonNegativeInt | None = None
    oidc_ssl_verify: bool = True
    oidc_use_session_secret: bool = True


class OLApisixOIDCResources(ComponentResource):
    """
    OIDC configuration for apisix
    Defines and creates an "OLVaultK8SSecret" resource in the k8s cluster
    Also provides helper functions for creating config blocks for the oidc plugin
    """

    def __init__(
        self,
        name: str,
        oidc_config: OLApisixOIDCConfig,
        opts: ResourceOptions | None = None,
    ):
        """Initialize the OLApisixOIDCResources component resource."""
        super().__init__(
            "ol:infrastructure:services:k8s:OLApisixOIDCResources", name, None, opts
        )

        resource_options = ResourceOptions(parent=self).merge(opts)

        self.secret_name = f"ol-apisix-{oidc_config.application_name}-oidc-secrets"

        __templates: dict[str, str | Output[str]] = {
            "client_id": '{{ get .Secrets "client_id" }}',
            "client_secret": '{{ get .Secrets "client_secret" }}',
            "realm": '{{ get .Secrets "realm_name" }}',
            "discovery": '{{ get .Secrets "url" }}/.well-known/openid-configuration',
        }

        if oidc_config.oidc_use_session_secret:
            __templates["session.secret"] = '{{ get .Secrets "secret" }}'

        self.oidc_secrets = OLVaultK8SSecret(
            f"{oidc_config.application_name}-oidc-secrets",
            resource_config=OLVaultK8SStaticSecretConfig(
                dest_secret_labels=oidc_config.k8s_labels,
                dest_secret_name=self.secret_name,
                exclude_raw=True,
                excludes=[".*"],
                labels=oidc_config.k8s_labels,
                mount=oidc_config.vault_mount,
                mount_type=oidc_config.vault_mount_type,
                name=self.secret_name,
                namespace=oidc_config.k8s_namespace,
                path=oidc_config.vault_path,
                refresh_after="1m",
                templates=__templates,
                vaultauth=oidc_config.vaultauth,
            ),
            opts=resource_options.merge(ResourceOptions(delete_before_replace=True)),
        )

        session_config: dict[str, Any] = {}
        # Flat session.cookie_domain, per the openid-connect plugin's
        # lua-resty-session 4.x schema on the pinned APISIX 3.17.0 (chart
        # 2.15.0). The nested session.cookie.domain form was the pre-3.17.0
        # shape; on 3.17.0+ it is a silent no-op since lua-resty-session 4.x
        # only reads the flat key.
        if oidc_config.oidc_session_cookie_domain:
            session_config.setdefault("session", {})["cookie_domain"] = (
                oidc_config.oidc_session_cookie_domain
            )

        # Flat session.cookie_name, same lua-resty-session 4.x schema as
        # cookie_domain above. Defaults to "session" in the plugin when unset.
        if oidc_config.oidc_session_cookie_name:
            session_config.setdefault("session", {})["cookie_name"] = (
                oidc_config.oidc_session_cookie_name
            )

        if oidc_config.oidc_session_absolute_timeout:
            session_config.setdefault("session", {})["absolute_timeout"] = (
                oidc_config.oidc_session_absolute_timeout
            )

        # Flat session.* keys, per the openid-connect plugin's lua-resty-session
        # 4.x schema (session.cookie.lifetime above is a deprecated 3.x alias
        # APISIX maps to absolute_timeout at runtime). Unlike cookie_lifetime,
        # 0 is a meaningful explicit value here (disables that timeout check),
        # so these are only emitted when the caller actually set them.
        if oidc_config.oidc_session_idling_timeout is not None:
            session_config.setdefault("session", {})["idling_timeout"] = (
                oidc_config.oidc_session_idling_timeout
            )

        if oidc_config.oidc_session_rolling_timeout is not None:
            session_config.setdefault("session", {})["rolling_timeout"] = (
                oidc_config.oidc_session_rolling_timeout
            )

        self.base_oidc_config = {
            "scope": oidc_config.oidc_scope,
            "bearer_only": oidc_config.oidc_bearer_only,
            "introspection_endpoint_auth_method": oidc_config.oidc_introspection_endpoint_auth_method,
            "ssl_verify": oidc_config.oidc_ssl_verify,
            "renew_access_token_on_expiry": oidc_config.oidc_renew_access_token_on_expiry,
            "logout_path": oidc_config.oidc_logout_path,
            "post_logout_redirect_uri": oidc_config.oidc_post_logout_redirect_uri,
            **session_config,
        }

        if oidc_config.oidc_session_contents:
            self.base_oidc_config["session_contents"] = (
                oidc_config.oidc_session_contents
            )

    def get_base_oidc_config(self, unauth_action: str) -> dict[str, Any]:
        """Return the base OIDC configuration dictionary."""
        return {
            **self.base_oidc_config,
            "unauth_action": unauth_action,
        }

    def get_full_oidc_plugin_config(self, unauth_action: str) -> dict[str, Any]:
        """Return the full OIDC plugin configuration dictionary for Apisix."""
        return {
            "name": "openid-connect",
            "enable": True,
            "secretRef": self.secret_name,
            "config": {
                **self.get_base_oidc_config(unauth_action),
            },
        }


# Ref: https://apisix.apache.org/docs/ingress-controller/references/apisix_pluginconfig_v2/
class OLApisixSharedPluginsConfig(BaseModel):
    """Configuration for OLApisixSharedPlugins.

    Defines the plugin list that will be materialised as both a v2
    ApisixPluginConfig (for legacy ApisixRoute/Ingress) and a v1alpha1
    PluginConfig (for Gateway API HTTPRoute).
    """

    application_name: str
    resource_suffix: str = "shared-plugins"
    enable_defaults: bool = True
    # Attach the opentelemetry plugin (OTLP trace export) to every route that
    # references this shared plugin config.  Automatically disabled on CI, where
    # the plugin is not loaded into APISIX and the Grafana Alloy collector does
    # not exist (see infrastructure/aws/eks/apisix_official.py).  A route that
    # references a plugin APISIX has not loaded is rejected, so the route-level
    # attachment must be gated to match the cluster-level plugin enablement.
    enable_opentelemetry: bool = True
    # Attach the gzip response-compression plugin.  On by default everywhere:
    # the plugin was loaded cluster-wide but referenced by no route for a long
    # time, so every shared-gateway response went out uncompressed.  Production
    # was gated behind a non-production soak until the CPU cost could be
    # measured; at 1.8 MB/s peak gateway egress, comp_level 1 costs a few
    # hundredths of a core, so the gate is gone.  Pass False to opt out an
    # application whose responses stream incrementally under a compressible
    # content type -- gzip buffering would delay their first byte.
    enable_gzip: bool = True
    k8s_labels: dict[str, str] = {}
    k8s_namespace: str
    # Either raw CRD dicts or OLApisixPluginConfig objects; the component
    # normalises the latter to dicts before rendering.
    plugins: list[dict[str, Any] | OLApisixPluginConfig] = []
    # Per-client-IP rate limiting as a DDoS backstop.  Opt-in (default off) so
    # that enabling it on one application does not change the behaviour of the
    # other services that share this component.  Limits are enforced per APISIX
    # pod (node-local shared memory), so the effective ceiling scales with the
    # gateway replica count; this is intended to throttle single-source floods,
    # not to replace edge/volumetric protection.
    enable_rate_limiting: bool = False
    # Key used to bucket requests.  ``remote_addr`` resolves to the real client
    # IP because the gateway trusts Fastly's X-Forwarded-For (see trustedAddresses).
    rate_limit_key: str = "remote_addr"
    rate_limit_rejected_code: int = 429
    # Thresholds are deliberately generous: large shared-NAT egress points (most
    # notably the MIT campus network) collapse many legitimate users onto a single
    # public IP, so a tight per-IP ceiling would throttle real traffic at peak.
    # These values target single-source flood abuse, not normal aggregated load.
    # limit-req: leaky-bucket request rate (requests/second) plus burst slack.
    rate_limit_requests_per_second: int = 300
    rate_limit_burst: int = 150
    # limit-conn: maximum concurrent in-flight requests plus burst slack.  A SPA
    # behind shared NAT can hold many parallel XHRs open, so this stays high.
    rate_limit_max_concurrent: int = 400
    rate_limit_concurrent_burst: int = 200


class OLApisixSharedPlugins(ComponentResource):
    """
    Shared plugin configuration for APISIX.

    Creates two Kubernetes CRD resources with the same ``metadata.name``:

    * ``apisix.apache.org/v2 / ApisixPluginConfig`` — consumed by legacy
      ``ApisixRoute`` and ``Ingress`` resources via ``plugin_config_name``.

    * ``apisix.apache.org/v1alpha1 / PluginConfig`` — consumed by Gateway API
      ``HTTPRoute`` resources via ExtensionRef filters (``kind: PluginConfig``).
      The v1alpha1 schema only accepts ``name`` and ``config`` per plugin;
      ``enable: false`` plugins are omitted entirely and ``secretRef`` is dropped.

    Callers can use ``self.resource_name`` in both ``OLApisixRouteConfig`` (legacy)
    and ``OLApisixHTTPRouteConfig`` (Gateway API) without any distinction.
    """

    def __init__(
        self,
        name: str,
        plugin_config: OLApisixSharedPluginsConfig,
        opts: ResourceOptions | None = None,
    ):
        """Initialize the OLApisixSharedPlugins component resource."""
        super().__init__(
            "ol:infrastructure:services:k8s:OLApisixSharedPlugin", name, None, opts
        )

        __default_plugins: list[dict[str, Any]] = [
            {
                "name": "redirect",
                "enable": True,
                "config": {
                    "http_to_https": True,
                },
            },
            {
                "name": "cors",
                "enable": True,
                "config": {
                    "allow_origins": "**",
                    "allow_methods": "**",
                    "allow_headers": "**",
                    "allow_credential": True,
                },
            },
            {
                "name": "response-rewrite",
                "enable": True,
                "config": {
                    "headers": {
                        "set": {
                            "Referrer-Policy": "origin",
                        }
                    },
                },
            },
            {
                "name": "prometheus",
                "enable": True,
                "config": {"prefer_name": True},
            },
        ]

        # Response compression.  APISIX loads the gzip plugin cluster-wide (see
        # the enabled-plugins list in infrastructure/aws/eks/apisix_official.py)
        # but until now it was attached to no route anywhere, so every response
        # through every shared gateway went out uncompressed.  Measured on
        # applications-production 2026-08-05: each JupyterHub
        # notebook_core.<hash>.js fetch was exactly 7,167,719 bytes, byte for
        # byte, on every single request -- ~4.85 GB/day of /static/ on
        # nb.learn.mit.edu alone.  Compressing shrinks both the egress and the
        # body NGINX has to buffer for a slow client, which is the mechanism
        # behind the 2026-07-21 gateway OOM (see the proxy_buffering block in
        # apisix_official.py).
        #
        # Kept out of __default_plugins so an application can opt out on its
        # own -- see ``enable_gzip`` on the config class.
        #
        # ``types`` deliberately lists only text-shaped payloads.  Formats that
        # are already compressed (images, video, woff/woff2, archives) would
        # burn CPU for no gain, and ``text/event-stream`` is excluded so SSE
        # responses are not held back by the compression buffers.
        #
        # ``comp_level`` stays at NGINX's own default of 1 rather than something
        # higher: this attaches to every route on a gateway whose HPA scales on
        # CPU, and level 1 already captures most of the reduction on JS/JSON.
        # Raise it only with gateway CPU headroom in hand.  ``vary`` is on so
        # downstream shared caches key on Accept-Encoding instead of serving a
        # gzipped body to a client that did not ask for one.
        __gzip_plugin: dict[str, Any] = {
            "name": "gzip",
            "enable": True,
            "config": {
                "types": [
                    "application/javascript",
                    "application/json",
                    "application/manifest+json",
                    "application/rss+xml",
                    "application/wasm",
                    "application/x-javascript",
                    "application/xml",
                    "image/svg+xml",
                    "text/css",
                    "text/html",
                    "text/javascript",
                    "text/plain",
                    "text/xml",
                ],
                # Below roughly one MTU the framing overhead outweighs the
                # saving, and APISIX's own default of 20 bytes would compress
                # essentially every response.
                "min_length": 1024,
                "comp_level": 1,
                # 16 x 4 KiB = 64 KiB of compression buffers per in-flight
                # response, deliberately in the same range as the ~72 KiB
                # proxy-buffer budget set in apisix_official.py so the
                # per-request memory footprint stays predictable.
                "buffers": {"number": 16, "size": 4096},
                "vary": True,
            },
        }

        # opentelemetry emits an OTLP trace span per request.  ``always_on`` head
        # sampling exports every span to the Grafana Alloy receiver, which then
        # applies tail sampling (keep errors, keep slow traces, probabilistic
        # remainder) before forwarding to Grafana Cloud (see grafana.py).  The
        # collector address and resource attributes are set cluster-wide via the
        # plugin's plugin_attr block in apisix_official.py.
        __opentelemetry_plugin: dict[str, Any] = {
            "name": "opentelemetry",
            "enable": True,
            "config": {"sampler": {"name": "always_on"}},
        }

        # limit-conn caps concurrent in-flight requests per client; limit-req
        # applies a leaky-bucket request-rate ceiling.  Both set
        # ``allow_degradation`` so that if the plugin's shared-memory store is
        # unavailable the request is allowed through rather than failing closed
        # — a rate-limiting backstop must never become its own outage.
        __rate_limit_plugins: list[dict[str, Any]] = [
            {
                "name": "limit-conn",
                "enable": True,
                "config": {
                    "conn": plugin_config.rate_limit_max_concurrent,
                    "burst": plugin_config.rate_limit_concurrent_burst,
                    "default_conn_delay": 0.1,
                    "key_type": "var",
                    "key": plugin_config.rate_limit_key,
                    "rejected_code": plugin_config.rate_limit_rejected_code,
                    "allow_degradation": True,
                },
            },
            {
                "name": "limit-req",
                "enable": True,
                "config": {
                    "rate": plugin_config.rate_limit_requests_per_second,
                    "burst": plugin_config.rate_limit_burst,
                    "nodelay": True,
                    "key_type": "var",
                    "key": plugin_config.rate_limit_key,
                    "rejected_code": plugin_config.rate_limit_rejected_code,
                    "allow_degradation": True,
                },
            },
        ]

        resource_options = ResourceOptions(parent=self).merge(opts)

        # Defaults first, then the caller's own plugins.  APISIX dispatches by
        # each plugin's registered priority rather than by array position, so
        # the order here is cosmetic -- but keeping the defaults at stable
        # indices means adding a plugin renders as a one-line append in
        # `pulumi preview` instead of shifting every entry down by one.
        plugins: list[dict[str, Any]] = (
            list(__default_plugins) if plugin_config.enable_defaults else []
        )
        plugins.extend(
            plugin.model_dump(by_alias=True, exclude_none=True)
            if isinstance(plugin, OLApisixPluginConfig)
            else plugin
            for plugin in plugin_config.plugins
        )
        if plugin_config.enable_rate_limiting:
            plugins.extend(__rate_limit_plugins)

        # Gate on CI to match the cluster-level plugin enablement: APISIX does not
        # load the opentelemetry plugin on CI, so attaching it to a route there
        # would cause the route config to be rejected.
        if (
            plugin_config.enable_opentelemetry
            and parse_stack().env_suffix.lower() != "ci"
        ):
            plugins.append(__opentelemetry_plugin)

        # No stack gate, unlike the opentelemetry check above: APISIX loads gzip
        # in every environment, so this is a per-application choice rather than a
        # correctness constraint.
        if plugin_config.enable_gzip:
            plugins.append(__gzip_plugin)

        self.resource_name = (
            f"{plugin_config.application_name}-{plugin_config.resource_suffix}"
        )
        # v2/ApisixPluginConfig — consumed by legacy ApisixRoute/Ingress resources
        # via ``plugin_config_name`` in the route spec.
        self.shared_plugin_apisix_pluginconfig_resource = (
            kubernetes.apiextensions.CustomResource(
                f"OLApisixSharedPlugin-{self.resource_name}",
                api_version="apisix.apache.org/v2",
                kind="ApisixPluginConfig",
                metadata=kubernetes.meta.v1.ObjectMetaArgs(
                    name=self.resource_name,
                    labels=plugin_config.k8s_labels,
                    namespace=plugin_config.k8s_namespace,
                ),
                spec={
                    "plugins": plugins,
                },
                opts=resource_options,
            )
        )
        # v1alpha1/PluginConfig — consumed by Gateway API HTTPRoute resources
        # via ExtensionRef filters (kind: PluginConfig).  Both resources share
        # the same metadata.name; callers pass self.resource_name to either
        # OLApisixRouteConfig (legacy) or OLApisixHTTPRouteConfig (Gateway API)
        # without distinction.
        #
        # The v1alpha1 schema only allows ``name`` and ``config`` per plugin;
        # ``enable`` and ``secretRef`` are v2-only fields.  Plugins with
        # ``enable: false`` are omitted so they don't appear in the Gateway API
        # path either.
        _v1alpha1_plugins = [
            {"name": p["name"], **({"config": p["config"]} if p.get("config") else {})}
            for p in plugins
            if p.get("enable", True)
        ]
        self.shared_plugin_pluginconfig_resource = (
            kubernetes.apiextensions.CustomResource(
                f"OLApisixSharedPluginConfig-{self.resource_name}",
                api_version="apisix.apache.org/v1alpha1",
                kind="PluginConfig",
                metadata=kubernetes.meta.v1.ObjectMetaArgs(
                    name=self.resource_name,
                    labels=plugin_config.k8s_labels,
                    namespace=plugin_config.k8s_namespace,
                ),
                spec={
                    "plugins": _v1alpha1_plugins,
                },
                opts=resource_options,
            )
        )


class OLApisixExternalUpstreamConfig(BaseModel):
    """Configuration for OLApisixExternalUpstream.

    Defines an external (non-Kubernetes) upstream service to be proxied
    through APISIX.
    """

    application_name: str
    resource_suffix: str = "external-upstream"
    k8s_labels: dict[str, str] = {}
    k8s_namespace: str
    external_hostname: str
    scheme: str = "https"


class OLApisixExternalUpstream(ComponentResource):
    """
    External upstream configuration for apisix
    Defines and creates an "ApisixUpstream" resource in the k8s cluster
    This is for a service that is hosted outside of kubernetes but we want
    to have APISIX in front of it anyways.
    """

    def __init__(
        self,
        name: str,
        external_upstream_config: OLApisixExternalUpstreamConfig,
        opts: ResourceOptions | None = None,
    ):
        """Initialize the OLApisixExternalUpstream component resource."""
        super().__init__(
            "ol:infrastructure:services:k8s:OLApisixExternalUpstream", name, None, opts
        )
        resource_options = ResourceOptions(parent=self).merge(opts)

        self.resource_name = f"{external_upstream_config.application_name}-{external_upstream_config.resource_suffix}"
        self.shared_plugin_apisix_pluginconfig_resource = (
            kubernetes.apiextensions.CustomResource(
                f"OLApisixExternalService-{self.resource_name}",
                api_version="apisix.apache.org/v2",
                kind="ApisixUpstream",
                metadata=kubernetes.meta.v1.ObjectMetaArgs(
                    name=self.resource_name,
                    labels=external_upstream_config.k8s_labels,
                    namespace=external_upstream_config.k8s_namespace,
                ),
                spec={
                    "scheme": external_upstream_config.scheme,
                    "externalNodes": [
                        {
                            "type": "Domain",
                            "name": external_upstream_config.external_hostname,
                        },
                    ],
                },
                opts=resource_options,
            )
        )


class OLApisixUpstreamConfig(BaseModel):
    """Configuration for OLApisixUpstream.

    Configures load-balancing behavior for an in-cluster Kubernetes Service
    upstream. ``service_name`` must match the target Service's name exactly —
    apisix-ingress-controller associates an ApisixUpstream resource with a
    Service by same-name, same-namespace lookup, not by an explicit
    reference from ApisixRoute.
    Ref: https://apisix.apache.org/docs/ingress-controller/concepts/apisix_upstream/
    """

    service_name: str
    k8s_namespace: str
    k8s_labels: dict[str, str] = {}
    loadbalancer_type: Literal["roundrobin", "chash", "ewma", "least_conn"] = (
        "roundrobin"
    )
    # Required when loadbalancer_type == "chash", e.g. hash_on="vars" with
    # hash_key="remote_addr" to consistently route a given client IP to the
    # same upstream pod.
    hash_on: Literal["vars", "header", "cookie", "consumer"] | None = None
    hash_key: str | None = None

    @model_validator(mode="after")
    def validate_chash_fields(self) -> "OLApisixUpstreamConfig":
        """Ensure hash_on/hash_key are set only when using chash load balancing."""
        if self.loadbalancer_type == "chash" and not (self.hash_on and self.hash_key):
            msg = "chash load balancing requires both hash_on and hash_key."
            raise ValueError(msg)
        if self.loadbalancer_type != "chash" and (self.hash_on or self.hash_key):
            msg = "hash_on/hash_key are only meaningful when loadbalancer_type='chash'."
            raise ValueError(msg)
        return self


class OLApisixUpstream(ComponentResource):
    """
    Load-balancing configuration for an in-cluster Service-backed upstream.
    Defines and creates an "ApisixUpstream" resource in the k8s cluster.
    """

    def __init__(
        self,
        name: str,
        upstream_config: OLApisixUpstreamConfig,
        opts: ResourceOptions | None = None,
    ):
        """Initialize the OLApisixUpstream component resource."""
        super().__init__(
            "ol:infrastructure:services:k8s:OLApisixUpstream", name, None, opts
        )
        resource_options = ResourceOptions(parent=self).merge(opts)

        loadbalancer_spec: dict[str, Any] = {"type": upstream_config.loadbalancer_type}
        if upstream_config.loadbalancer_type == "chash":
            loadbalancer_spec["hashOn"] = upstream_config.hash_on
            loadbalancer_spec["key"] = upstream_config.hash_key

        self.apisix_upstream_resource = kubernetes.apiextensions.CustomResource(
            f"OLApisixUpstream-{name}",
            api_version="apisix.apache.org/v2",
            kind="ApisixUpstream",
            metadata=kubernetes.meta.v1.ObjectMetaArgs(
                # Must equal the target Service's name — see class docstring.
                name=upstream_config.service_name,
                labels=upstream_config.k8s_labels,
                namespace=upstream_config.k8s_namespace,
            ),
            spec={"loadbalancer": loadbalancer_spec},
            opts=resource_options,
        )
