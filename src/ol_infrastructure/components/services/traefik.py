"""Traefik ingress controller components for Kubernetes."""

from typing import Any

import pulumi_kubernetes as kubernetes
from pulumi import ComponentResource, ResourceOptions


class OLTraefikMiddleware(ComponentResource):
    """
    Generic component for creating Traefik Middleware custom resources.
    """

    def __init__(
        self,
        name: str,
        middleware_name: str,
        namespace: str,
        spec: dict[str, Any],
        opts: ResourceOptions | None = None,
    ):
        """Initialize the OLTraefikMiddleware component resource."""
        super().__init__(
            "ol:infrastructure:services:k8s:OLTraefikMiddleware", name, None, opts
        )
        resource_options = ResourceOptions(parent=self).merge(opts)

        self.traefik_middleware = kubernetes.apiextensions.CustomResource(
            f"OLTraefikMiddleware-{name}",
            api_version="traefik.io/v1alpha1",
            kind="Middleware",
            metadata={
                "name": middleware_name,
                "namespace": namespace,
            },
            spec=spec,
            opts=resource_options,
        )
        self.gateway_filter = {
            "type": "ExtensionRef",
            "extensionRef": {
                "group": "traefik.io",
                "kind": "Middleware",
                "name": middleware_name,
            },
        }
