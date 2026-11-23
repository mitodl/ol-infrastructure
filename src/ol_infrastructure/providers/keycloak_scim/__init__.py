"""scim-for-keycloak plugin resources for Pulumi."""

from .remote_provider import (
    ScimAuthentication,
    ScimRemoteProvider,
    ScimRemoteProviderProvider,
    ScimRemoteProviderSpec,
    ScimUserAttribute,
)

__all__ = [
    "ScimAuthentication",
    "ScimRemoteProvider",
    "ScimRemoteProviderProvider",
    "ScimRemoteProviderSpec",
    "ScimUserAttribute",
]
