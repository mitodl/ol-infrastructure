# ruff: noqa: E501
"""
APISIX Gateway API components for HTTPRoute-based routing.

This module provides Gateway API (HTTPRoute) components that work with the APISIX
Ingress Controller, serving as the modern replacement for ApisixRoute CRDs.
"""

import hashlib
from typing import Any, Literal

import pulumi_kubernetes as kubernetes
from pulumi import ComponentResource, ResourceOptions
from pydantic import (
    BaseModel,
    NonNegativeInt,
    field_validator,
    model_validator,
)

from ol_infrastructure.components.services.apisix import OLApisixPluginConfig


class OLApisixHTTPRouteConfig(BaseModel):
    """Configuration for a single HTTPRoute rule (Gateway API).

    Note on WebSocket support:
        WebSocket uses HTTP Upgrade protocol (RFC 6455) - clients send HTTP requests
        with Upgrade headers, and servers respond with 101 Switching Protocols.

        Contrary to GEP-1911's guidance that gateways SHOULD upgrade WebSockets
        even without a hint, the APISIX ingress controller (verified on 2.1.0 /
        APISIX 3.16.0) does NOT auto-upgrade: WebSocket Upgrade requests routed to
        a plain HTTP backend are forwarded as ordinary GETs, which surfaces as
        WebSocket-only endpoints returning 404. APISIX only enables the upgrade
        when the backend Service referenced by the HTTPRoute carries
        ``appProtocol: kubernetes.io/ws`` on the target port.

        Because that hint lives on the Service (not the HTTPRoute/backendRef) and
        the real backend Service is frequently owned by a Helm chart that cannot
        set appProtocol on its main port (e.g. z2jh proxy-public), setting
        ``websocket=True`` here makes this component create a dedicated, sibling
        Service named ``<backend_service_name>-ws`` that selects the same pods via
        ``websocket_backend_selector`` and exposes the port with
        ``appProtocol: kubernetes.io/ws``. The generated HTTPRoute backendRef is
        rewritten to point at that ``-ws`` Service. This keeps ownership clean (the
        component owns the WebSocket-enabled Service) without patching Helm-managed
        resources.

        ``websocket`` only applies to service backends; it is ignored for
        ``upstream`` backends (APISIX upstreams configure WebSocket separately).

        Reference: https://kubernetes.io/blog/2024/11/21/gateway-api-v1-2/
        GEP-1911: https://gateway-api.sigs.k8s.io/geps/gep-1911/
        APISIX appProtocol/WebSocket: https://apisix.apache.org/docs/ingress-controller/concepts/gateway-api/

    Note on priority:
        The Gateway API HTTPRoute does not have a native 'priority' field.
        Route precedence is determined by match specificity:
        - Exact path matches take precedence over prefix matches
        - Longer prefix matches take precedence over shorter prefix matches
        - Multiple routes with identical matches have undefined precedence

        The 'priority' field is retained for compatibility with ApisixRoute but
        has NO effect on HTTPRoute generation. To control route precedence, use
        more specific path matches (e.g., /api/ws/ before /api/).

    Note on backend_resolve_granularity:
        This is an APISix-specific field from ApisixRoute CRD. The Gateway API
        HTTPRoute uses standard Kubernetes Service discovery (endpoints). This
        field is retained for compatibility but has NO effect on HTTPRoute generation.
    """

    route_name: str
    priority: int = 0  # NOT used in Gateway API HTTPRoute - see class docstring
    shared_plugin_config_name: str | None = None
    plugins: list[OLApisixPluginConfig] = []
    hosts: list[str] = []
    paths: list[str] = []
    backend_service_name: str | None = None
    backend_service_port: str | NonNegativeInt | None = None
    backend_resolve_granularity: Literal["endpoint", "service"] = (
        "service"  # NOT used in Gateway API HTTPRoute
    )
    upstream: str | None = None
    websocket: bool = False  # Service backends only: creates a <name>-ws Service with appProtocol=kubernetes.io/ws - see class docstring
    # Pod selector for the generated WebSocket Service. Required when
    # websocket=True with a service backend; must match the backend pods (e.g.
    # the same selector the backend Service uses).
    websocket_backend_selector: dict[str, str] | None = None
    # targetPort for the generated WebSocket Service. Defaults to
    # backend_service_port when omitted; set this when the backend Service maps a
    # numeric port to a named targetPort (e.g. proxy-public 80 -> "http").
    websocket_backend_target_port: str | NonNegativeInt | None = None

    @field_validator("plugins")
    @classmethod
    def ensure_request_id_plugin(
        cls, v: list[OLApisixPluginConfig]
    ) -> list[OLApisixPluginConfig]:
        """Ensure that the request-id plugin is always added to the plugins list."""
        if not any(plugin.name == "request-id" for plugin in v):
            v.append(
                OLApisixPluginConfig(
                    name="request-id",
                    secretRef=None,
                    config={"include_in_response": True},
                )
            )
        return v

    @model_validator(mode="after")
    def check_backend_or_upstream(self) -> "OLApisixHTTPRouteConfig":
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

    @model_validator(mode="after")
    def check_websocket_selector(self) -> "OLApisixHTTPRouteConfig":
        """Require a pod selector when WebSocket support is enabled on a service.

        WebSocket support is implemented by creating a sibling Service with
        ``appProtocol: kubernetes.io/ws`` that selects the backend pods directly,
        so the selector is mandatory. ``websocket`` has no effect on ``upstream``
        backends, so it is left untouched there.
        """
        if (
            self.websocket
            and self.upstream is None
            and not self.websocket_backend_selector
        ):
            msg = "When 'websocket' is True with a service backend, 'websocket_backend_selector' must be provided."
            raise ValueError(msg)
        return self


class OLApisixHTTPRoute(ComponentResource):
    """
    HTTPRoute configuration for Gateway API with APISIX.

    Creates HTTPRoute and associated PluginConfig (apisix.apache.org/v1alpha1)
    resources for routing traffic through the Gateway API.  This is the modern
    replacement for ApisixRoute.

    NOTE ON CRD KINDS
    -----------------
    APISIX exposes two plugin-config CRD types:

    * ``apisix.apache.org/v2 / ApisixPluginConfig`` — for legacy ApisixRoute and
      Kubernetes Ingress resources only.  The APISIX ingress-controller's HTTPRoute
      reconciler does **not** read this kind when processing ExtensionRef filters;
      references to it are silently ignored.

    * ``apisix.apache.org/v1alpha1 / PluginConfig`` — the Gateway-API-aligned
      type that the HTTPRoute reconciler **does** process via ExtensionRef filters.
      This component uses this kind exclusively.

    The component automatically:
    - Creates PluginConfig (v1alpha1) resources for unique plugin combinations
    - Converts APISIX path patterns to Gateway API PathPrefix matches
    - Links routes to plugins via ExtensionRef filters
    - Supports both service backends and upstreams
    """

    def __init__(  # noqa: PLR0913
        self,
        name: str,
        route_configs: list[OLApisixHTTPRouteConfig],
        k8s_namespace: str,
        k8s_labels: dict[str, str],
        gateway_name: str = "apisix",
        gateway_namespace: str = "operations",
        opts: ResourceOptions | None = None,
    ):
        """
        Initialize the OLApisixHTTPRoute component resource.

        Args:
            name: Resource name for the HTTPRoute
            route_configs: List of route configurations
            k8s_namespace: Kubernetes namespace for the HTTPRoute
            k8s_labels: Labels to apply to all resources
            gateway_name: Name of the Gateway resource to attach to
            gateway_namespace: Namespace of the Gateway resource
            opts: Pulumi resource options
        """
        super().__init__(
            "ol:infrastructure:services:k8s:OLApisixHTTPRoute", name, None, opts
        )

        resource_options = ResourceOptions(parent=self).merge(opts)

        # Create PluginConfig resources for unique plugin combinations
        self._create_plugin_configs(
            name, route_configs, k8s_namespace, k8s_labels, resource_options
        )

        # Create WebSocket-enabled (<name>-ws) Services for routes with
        # websocket=True so APISIX upgrades the connection (appProtocol hint).
        self._create_websocket_services(
            route_configs, k8s_namespace, k8s_labels, resource_options
        )

        # Build HTTPRoute rules
        http_rules = self._build_http_route_rules(name, route_configs)

        # Build HTTPRoute spec - omit hostnames field if empty per Gateway API spec
        hostnames = self._extract_unique_hostnames(route_configs)
        spec: dict[str, Any] = {
            "parentRefs": [
                {
                    "name": gateway_name,
                    "namespace": gateway_namespace,
                }
            ],
            "rules": http_rules,
        }
        if hostnames:  # Only include hostnames if we have any
            spec["hostnames"] = hostnames

        # Create the HTTPRoute resource
        self.http_route_resource = kubernetes.apiextensions.CustomResource(
            f"OLApisixHTTPRoute-{name}",
            api_version="gateway.networking.k8s.io/v1",
            kind="HTTPRoute",
            metadata=kubernetes.meta.v1.ObjectMetaArgs(
                name=name,
                labels=k8s_labels,
                namespace=k8s_namespace,
            ),
            spec=spec,
            opts=resource_options.merge(ResourceOptions(delete_before_replace=True)),
        )

    def _extract_unique_hostnames(
        self, route_configs: list[OLApisixHTTPRouteConfig]
    ) -> list[str]:
        """Extract unique hostnames from all route configs for external-dns."""
        hostnames = set()
        for route_config in route_configs:
            for host in route_config.hosts:
                if host:  # Skip empty strings
                    hostnames.add(host)
        return sorted(hostnames)  # Sort for deterministic output

    def _generate_plugin_config_name(
        self, httproute_name: str, route_name: str, plugins: list[OLApisixPluginConfig]
    ) -> str:
        """Generate a unique name for a plugin configuration.

        Args:
            httproute_name: Name of the parent HTTPRoute (ensures uniqueness across routes)
            route_name: Name of the specific route config
            plugins: List of plugins to hash
        """
        # Create a hash of the plugin configuration for uniqueness
        plugin_data = str([p.model_dump() for p in plugins])
        plugin_hash = hashlib.sha256(plugin_data.encode()).hexdigest()[:8]
        # Sanitize names to comply with RFC 1123 subdomain rules (lowercase alphanumeric + hyphens)
        sanitized_httproute_name = httproute_name.replace("_", "-").lower()
        sanitized_route_name = route_name.replace("_", "-").lower()
        return f"{sanitized_httproute_name}-{sanitized_route_name}-{plugin_hash}"

    @staticmethod
    def _active_plugins(
        plugins: list[OLApisixPluginConfig],
    ) -> list[OLApisixPluginConfig]:
        """Return only plugins where ``enable`` is True.

        v1alpha1 PluginConfig has no ``enable`` flag, so disabled plugins must
        be dropped rather than serialised.  Using the same filtered list for
        both name-hashing and spec-generation keeps the PluginConfig resource
        name consistent with its contents.
        """
        return [p for p in plugins if p.enable]

    @staticmethod
    def _serialize_plugin_for_v1alpha1(
        plugin: OLApisixPluginConfig,
    ) -> dict[str, Any]:
        """Serialise an OLApisixPluginConfig for the v1alpha1 PluginConfig CRD.

        The v1alpha1 PluginConfig schema only accepts ``name`` and ``config``
        fields — it has no ``enable`` or ``secretRef`` fields (those belong to
        the legacy v2 ApisixPluginConfig CRD used with ApisixRoute/Ingress).
        """
        result: dict[str, Any] = {"name": plugin.name}
        if plugin.config:  # omit empty config dicts
            result["config"] = plugin.config
        return result

    def _create_plugin_configs(
        self,
        httproute_name: str,
        route_configs: list[OLApisixHTTPRouteConfig],
        k8s_namespace: str,
        k8s_labels: dict[str, str],
        resource_options: ResourceOptions,
    ) -> dict[str, kubernetes.apiextensions.CustomResource]:
        """Create PluginConfig (apisix.apache.org/v1alpha1) resources for unique plugin combinations.

        Uses v1alpha1/PluginConfig — the correct CRD for Gateway API HTTPRoute
        ExtensionRef filters.  The v2/ApisixPluginConfig CRD is for legacy
        ApisixRoute/Ingress resources only and is silently ignored by the
        HTTPRoute reconciler.
        """
        plugin_configs = {}

        for route_config in route_configs:
            # Skip if using shared plugin config
            if route_config.shared_plugin_config_name:
                continue

            # Skip if no plugins (shouldn't happen due to validator adding request-id)
            if not route_config.plugins:
                continue

            # Only enabled plugins are written to v1alpha1 PluginConfig.
            active = self._active_plugins(route_config.plugins)
            if not active:
                continue

            # Generate unique name for this plugin combination
            config_name = self._generate_plugin_config_name(
                httproute_name, route_config.route_name, active
            )

            # Only create if we haven't seen this exact plugin combo before
            if config_name not in plugin_configs:
                plugin_configs[config_name] = kubernetes.apiextensions.CustomResource(
                    f"PluginConfig-{config_name}",
                    api_version="apisix.apache.org/v1alpha1",
                    kind="PluginConfig",
                    metadata=kubernetes.meta.v1.ObjectMetaArgs(
                        name=config_name,
                        labels=k8s_labels,
                        namespace=k8s_namespace,
                    ),
                    spec={
                        "plugins": [
                            self._serialize_plugin_for_v1alpha1(p) for p in active
                        ]
                    },
                    opts=resource_options,
                )

        return plugin_configs

    @staticmethod
    def _resolve_backend_port(port: str | int | None) -> int:
        """Resolve a Service port to the numeric value Gateway API requires.

        Gateway API backendRef ports must be numeric. Named ports are mapped to
        APISIX's conventional defaults, matching the logic used when building
        backendRefs.
        """
        if isinstance(port, str):
            port_mapping = {
                "http": 8071,  # DEFAULT_NGINX_PORT
                "https": 443,
                "http-alt": 8080,
            }
            return port_mapping.get(port, 8071)
        return port if port is not None else 8071

    @staticmethod
    def _ws_service_name(backend_service_name: str) -> str:
        """Name of the WebSocket-enabled sibling Service for a backend."""
        return f"{backend_service_name}-ws"

    def _create_websocket_services(
        self,
        route_configs: list[OLApisixHTTPRouteConfig],
        k8s_namespace: str,
        k8s_labels: dict[str, str],
        resource_options: ResourceOptions,
    ) -> dict[str, kubernetes.core.v1.Service]:
        """Create ``<backend>-ws`` Services for routes with WebSocket enabled.

        APISIX only upgrades WebSocket connections when the backend Service port
        advertises ``appProtocol: kubernetes.io/ws``. Since the real backend
        Service is often Helm-owned and cannot set that hint on its main port,
        this creates a component-owned sibling Service that selects the same pods
        and carries the hint. The HTTPRoute backendRef is later pointed at it.
        """
        ws_services: dict[str, kubernetes.core.v1.Service] = {}

        for route_config in route_configs:
            # WebSocket support only applies to service backends.
            if not route_config.websocket or route_config.upstream:
                continue

            backend_name = route_config.backend_service_name
            # Guaranteed non-None for service backends by the model validators.
            if backend_name is None:
                continue

            ws_name = self._ws_service_name(backend_name)
            # De-dup when multiple routes share the same backend Service.
            if ws_name in ws_services:
                continue

            port = self._resolve_backend_port(route_config.backend_service_port)
            target_port: str | int = (
                route_config.websocket_backend_target_port
                if route_config.websocket_backend_target_port is not None
                else port
            )

            ws_services[ws_name] = kubernetes.core.v1.Service(
                f"OLApisixHTTPRoute-ws-service-{ws_name}",
                metadata=kubernetes.meta.v1.ObjectMetaArgs(
                    name=ws_name,
                    namespace=k8s_namespace,
                    labels=k8s_labels,
                ),
                spec=kubernetes.core.v1.ServiceSpecArgs(
                    type="ClusterIP",
                    selector=route_config.websocket_backend_selector,
                    ports=[
                        kubernetes.core.v1.ServicePortArgs(
                            name="ws",
                            port=port,
                            target_port=target_port,
                            protocol="TCP",
                            # The hint that makes APISIX perform the HTTP Upgrade.
                            app_protocol="kubernetes.io/ws",
                        )
                    ],
                ),
                opts=resource_options,
            )

        return ws_services

    def _build_http_route_rules(
        self,
        httproute_name: str,
        route_configs: list[OLApisixHTTPRouteConfig],
    ) -> list[dict[str, Any]]:
        """Build HTTPRoute rules from route configurations."""
        rules = []

        for route_config in route_configs:
            # Build matches for paths - default to "/" if no paths specified
            matches = []
            paths = route_config.paths if route_config.paths else ["/"]
            for path in paths:
                # Convert APISIX path pattern to Gateway API path match
                # APISIX uses /path/* format, Gateway API uses prefix matching
                path_value = path.rstrip("*").rstrip("/")
                if not path_value:
                    path_value = "/"

                match = {
                    "path": {
                        "type": "PathPrefix",
                        "value": path_value,
                    }
                }

                matches.append(match)

            # Build backend refs
            if route_config.upstream:
                # For upstream references, use the upstream name
                backend_refs = [{"name": route_config.upstream}]
            else:
                # For service references - these are guaranteed to be non-None by validator
                # Gateway API requires port to be numeric, not a string port name
                port = self._resolve_backend_port(route_config.backend_service_port)

                # When WebSocket is enabled, route to the component-owned
                # <backend>-ws Service (appProtocol: kubernetes.io/ws) instead of
                # the plain backend, so APISIX performs the HTTP Upgrade.
                backend_name = route_config.backend_service_name
                if route_config.websocket and backend_name is not None:
                    backend_name = self._ws_service_name(backend_name)

                backend_ref: dict[str, Any] = {
                    "name": backend_name,
                    "port": port,
                }
                backend_refs = [backend_ref]

            # Build the rule
            rule: dict[str, Any] = {
                "matches": matches,
                "backendRefs": backend_refs,
            }

            # Add plugin config via ExtensionRef filter if plugins are configured.
            # Both paths use kind=PluginConfig (apisix.apache.org/v1alpha1) — the
            # type the APISIX HTTPRoute reconciler actually processes.  The legacy
            # v2/ApisixPluginConfig kind is silently ignored by the reconciler.
            if route_config.shared_plugin_config_name:
                rule["filters"] = [
                    {
                        "type": "ExtensionRef",
                        "extensionRef": {
                            "group": "apisix.apache.org",
                            "kind": "PluginConfig",
                            "name": route_config.shared_plugin_config_name,
                        },
                    }
                ]
            elif route_config.plugins:
                # Use the same enabled-plugin subset that was used to create
                # the PluginConfig resource so the name always matches.
                active = self._active_plugins(route_config.plugins)
                if active:
                    config_name = self._generate_plugin_config_name(
                        httproute_name, route_config.route_name, active
                    )
                    rule["filters"] = [
                        {
                            "type": "ExtensionRef",
                            "extensionRef": {
                                "group": "apisix.apache.org",
                                "kind": "PluginConfig",
                                "name": config_name,
                            },
                        }
                    ]

            rules.append(rule)

        return rules
