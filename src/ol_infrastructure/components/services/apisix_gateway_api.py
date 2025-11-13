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

from ol_infrastructure.components.services.k8s import OLApisixPluginConfig


class OLApisixHTTPRouteConfig(BaseModel):
    """Configuration for a single HTTPRoute rule (Gateway API)."""

    route_name: str
    priority: int = 0
    shared_plugin_config_name: str | None = None
    plugins: list[OLApisixPluginConfig] = []
    hosts: list[str] = []
    paths: list[str] = []
    backend_service_name: str | None = None
    backend_service_port: str | NonNegativeInt | None = None
    backend_resolve_granularity: Literal["endpoint", "service"] = "service"
    upstream: str | None = None
    websocket: bool = False

    @field_validator("plugins")
    @classmethod
    def ensure_request_id_plugin(
        cls, v: list[OLApisixPluginConfig]
    ) -> list[OLApisixPluginConfig]:
        """Ensure that the request-id plugin is always added to the plugins list."""
        if not any(plugin.name == "request-id" for plugin in v):
            v.append(
                OLApisixPluginConfig(
                    name="request-id", config={"include_in_response": True}
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


class OLApisixHTTPRoute(ComponentResource):
    """
    HTTPRoute configuration for Gateway API with APISIX.

    Creates HTTPRoute and associated ApisixPluginConfig resources for routing
    traffic through the Gateway API. This is the modern replacement for ApisixRoute.

    The component automatically:
    - Creates ApisixPluginConfig resources for unique plugin combinations
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
        plugin_configs = self._create_plugin_configs(
            route_configs, k8s_namespace, k8s_labels, resource_options
        )

        # Build HTTPRoute rules
        http_rules = self._build_http_route_rules(route_configs)

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
            spec={
                "parentRefs": [
                    {
                        "name": gateway_name,
                        "namespace": gateway_namespace,
                    }
                ],
                "hostnames": self._extract_unique_hostnames(route_configs),
                "rules": http_rules,
            },
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
        self, route_name: str, plugins: list[OLApisixPluginConfig]
    ) -> str:
        """Generate a unique name for a plugin configuration."""
        # Create a hash of the plugin configuration for uniqueness
        plugin_data = str([p.model_dump() for p in plugins])
        plugin_hash = hashlib.sha256(plugin_data.encode()).hexdigest()[:8]
        return f"{route_name}-plugins-{plugin_hash}"

    def _create_plugin_configs(
        self,
        route_configs: list[OLApisixHTTPRouteConfig],
        k8s_namespace: str,
        k8s_labels: dict[str, str],
        resource_options: ResourceOptions,
    ) -> dict[str, kubernetes.apiextensions.CustomResource]:
        """Create ApisixPluginConfig resources for unique plugin combinations."""
        plugin_configs = {}

        for route_config in route_configs:
            # Skip if using shared plugin config
            if route_config.shared_plugin_config_name:
                continue

            # Skip if no plugins (shouldn't happen due to validator adding request-id)
            if not route_config.plugins:
                continue

            # Generate unique name for this plugin combination
            config_name = self._generate_plugin_config_name(
                route_config.route_name, route_config.plugins
            )

            # Only create if we haven't seen this exact plugin combo before
            if config_name not in plugin_configs:
                plugin_configs[config_name] = kubernetes.apiextensions.CustomResource(
                    f"ApisixPluginConfig-{config_name}",
                    api_version="apisix.apache.org/v2",
                    kind="ApisixPluginConfig",
                    metadata=kubernetes.meta.v1.ObjectMetaArgs(
                        name=config_name,
                        labels=k8s_labels,
                        namespace=k8s_namespace,
                    ),
                    spec={
                        "plugins": [
                            p.model_dump(by_alias=True) for p in route_config.plugins
                        ]
                    },
                    opts=resource_options,
                )

        return plugin_configs

    def _build_http_route_rules(
        self,
        route_configs: list[OLApisixHTTPRouteConfig],
    ) -> list[dict[str, Any]]:
        """Build HTTPRoute rules from route configurations."""
        rules = []

        for route_config in route_configs:
            # Build matches for paths
            matches = []
            for path in route_config.paths:
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
                assert route_config.backend_service_name is not None
                assert route_config.backend_service_port is not None
                backend_ref: dict[str, Any] = {
                    "name": route_config.backend_service_name,
                    "port": route_config.backend_service_port,
                }
                backend_refs = [backend_ref]

            # Build the rule
            rule: dict[str, Any] = {
                "matches": matches,
                "backendRefs": backend_refs,
            }

            # Add plugin config via ExtensionRef filter if plugins are configured
            if route_config.shared_plugin_config_name:
                rule["filters"] = [
                    {
                        "type": "ExtensionRef",
                        "extensionRef": {
                            "group": "apisix.apache.org",
                            "kind": "ApisixPluginConfig",
                            "name": route_config.shared_plugin_config_name,
                        },
                    }
                ]
            elif route_config.plugins:
                config_name = self._generate_plugin_config_name(
                    route_config.route_name, route_config.plugins
                )
                rule["filters"] = [
                    {
                        "type": "ExtensionRef",
                        "extensionRef": {
                            "group": "apisix.apache.org",
                            "kind": "ApisixPluginConfig",
                            "name": config_name,
                        },
                    }
                ]

            rules.append(rule)

        return rules
