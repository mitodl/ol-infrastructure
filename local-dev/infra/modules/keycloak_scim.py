"""Outbound SCIM provisioning from the local-dev Keycloak to the local apps.

The service account comes from the deployed stacks' create_scim_config_manager
(substructure/keycloak/scim.py), minus the Vault copy of its credentials, which
local-dev has no use for.

Keycloak pushes user create/update/delete to each app's inbound SCIM endpoint.
The targets are in-cluster Service URLs over plain HTTP, which keeps the
Keycloak pod out of APISIX's http_to_https redirect and away from the mkcert
trust problem. Each app authenticates the push with a static bearer token that
must also exist in its own database; local-dev/scripts/seed-scim-tokens.sh
writes those rows.

The plugin creates the scim-admin client roles during its own per-realm
initialisation, so the lookups here only resolve once Keycloak is up with the
SPI options from identity_core.py and the olapps realm exists.
"""

from typing import Any

import pulumi_keycloak as keycloak
from pulumi import ResourceOptions

from bridge.lib.magic_numbers import DEFAULT_WSGI_PORT
from ol_infrastructure.applications.mitxonline import definition as mitxonline
from ol_infrastructure.providers.keycloak_scim import (
    ScimAuthentication,
    ScimRemoteProvider,
    ScimRemoteProviderSpec,
    ScimUserAttribute,
)
from ol_infrastructure.substructure.keycloak.scim import create_scim_config_manager

LEARN_SCIM_BASE_URL = "http://mitlearn-webapp.mit-learn.svc.cluster.local:8060/scim/v2"
# mitxonline's application_config sets import_nginx_config=False and no
# application_port, so OLApplicationK8s serves the webapp Service on
# DEFAULT_WSGI_PORT (tests/local_dev pins that).
MITXONLINE_SCIM_BASE_URL = (
    f"http://{mitxonline.WEBAPP_SERVICE_NAME}.{mitxonline.NAMESPACE}"
    f".svc.cluster.local:{DEFAULT_WSGI_PORT}/scim/v2"
)

# Each provider's capability flags and custom User-schema mappings, the same as
# the Production provider for that app (substructure/keycloak/
# Pulumi.Production.yaml), so local-dev sends the request shapes Production
# does. Every flag is declared, off ones included, because loading the default
# configuration resets undeclared ones to RFC defaults. Production's mitxonline
# provider sends bulk requests on Learn's limits but no sort parameters, and
# maps neither fullName nor emailOptIn, so a Keycloak name change does not
# reach mitxonline's name here either; add fullName to its list to test that.
COMMON_CAPABILITIES: dict[str, Any] = {
    "filter_supported": True,
    "filter_max_results": 50,
    "patch_supported": True,
    "etag_supported": False,
    "change_password_supported": False,
}
LEARN_PROVIDER: dict[str, Any] = {
    **COMMON_CAPABILITIES,
    "sort_supported": True,
    "bulk_supported": True,
    "bulk_max_operations": 100,
    "bulk_max_payload_size": 1048576,
    "user_attributes": [
        ScimUserAttribute(
            name="fullName", type="string", custom_attribute_name="fullName"
        ),
        ScimUserAttribute(
            name="emailOptIn", type="integer", custom_attribute_name="emailOptIn"
        ),
    ],
}
MITXONLINE_PROVIDER: dict[str, Any] = {
    **COMMON_CAPABILITIES,
    "sort_supported": False,
    "bulk_supported": True,
    "bulk_max_operations": 100,
    "bulk_max_payload_size": 1048576,
    "user_attributes": [],
}


def create_scim_remote_providers(  # noqa: PLR0913
    keycloak_provider: keycloak.Provider,
    keycloak_url: str,
    realm: keycloak.Realm,
    learn_scim_token: str,
    mitxonline_scim_token: str,
    *,
    realm_name: str,
    enabled_apps: tuple[str, ...] = (),
) -> None:
    """Register the SCIM service account and a remote provider per enabled app.

    :param keycloak_provider: Provider authenticated against the master realm.
    :param keycloak_url: Base URL the host reaches Keycloak on. The plugin
        compares it against the request URL, which is why identity_core.py sets
        admin-url-check to no-context-path.
    :param realm: The olapps realm, depended on so the per-realm scim-admin
        role exists before it is granted.
    :param learn_scim_token: Bearer token mit-learn will accept. Must match the
        AccessToken row seeded in mit-learn's database.
    :param mitxonline_scim_token: Bearer token mitxonline will accept.
    :param realm_name: Name of the realm whose users are provisioned.
    :param enabled_apps: Which apps the stack is running. A provider is created
        only for an app that is on: its target Service does not exist otherwise,
        and with replay_on_failure the plugin would queue every failed push for
        retry indefinitely. Mirrors the gating on the per-app clients in
        keycloak.py.
    """
    scim_client, role_grants = create_scim_config_manager(
        keycloak_provider, [realm_name], depends_on=[realm]
    )

    # Keyed by the app name the Tiltfile uses, so enabled_apps gates them.
    targets = {
        "mit-learn": (
            "ol-learn-scim",
            LEARN_SCIM_BASE_URL,
            learn_scim_token,
            LEARN_PROVIDER,
        ),
        "mitxonline": (
            "ol-mitxonline-scim",
            MITXONLINE_SCIM_BASE_URL,
            mitxonline_scim_token,
            MITXONLINE_PROVIDER,
        ),
    }

    for app, (name, base_url, token, provider) in targets.items():
        if app not in set(enabled_apps):
            continue
        ScimRemoteProvider(
            f"{realm_name}-scim-remote-provider-{name}",
            keycloak_url=keycloak_url,
            client_id=scim_client.client_id,
            client_secret=scim_client.client_secret,
            spec=ScimRemoteProviderSpec(
                realm=realm_name,
                name=name,
                base_url=base_url,
                enabled=True,
                # Keep failed pushes queued, so a target that was down while
                # Tilt rebuilt it still receives them.
                replay_on_failure=True,
                authentication_list=[
                    ScimAuthentication(
                        authentication_type="BEARER",
                        authentication_token=token,
                    )
                ],
                **provider,
            ),
            opts=ResourceOptions(depends_on=[*role_grants, realm]),
        )


__all__ = ["create_scim_remote_providers"]
