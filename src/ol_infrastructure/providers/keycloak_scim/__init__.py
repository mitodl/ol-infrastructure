"""scim-for-keycloak plugin resources for Pulumi."""

from .remote_provider import (
    ScimAuthentication,
    ScimRemoteProvider,
    ScimRemoteProviderProvider,
    ScimRemoteProviderSpec,
    ScimUserAttribute,
)
from .service_provider import (
    ScimServiceProviderConfig,
    ScimServiceProviderConfigProvider,
    ScimServiceProviderSpec,
)

__all__ = [
    "ScimAuthentication",
    "ScimRemoteProvider",
    "ScimRemoteProviderProvider",
    "ScimRemoteProviderSpec",
    "ScimServiceProviderConfig",
    "ScimServiceProviderConfigProvider",
    "ScimServiceProviderSpec",
    "ScimUserAttribute",
]
