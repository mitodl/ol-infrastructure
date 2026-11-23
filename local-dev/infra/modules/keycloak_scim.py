"""Outbound SCIM provisioning from the local-dev Keycloak to the local apps.

Mirrors substructure/keycloak/scim.py, minus Vault: the service account's
credentials are passed straight to the remote-provider resource rather than
published for an export script, so nothing reads them back.

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

import pulumi_keycloak as keycloak
from pulumi import InvokeOptions, ResourceOptions

from ol_infrastructure.providers.keycloak_scim import (
    ScimAuthentication,
    ScimRemoteProvider,
    ScimRemoteProviderSpec,
    ScimUserAttribute,
)

SCIM_ADMIN_ROLE = "scim-admin"
MASTER_REALM = "master"
# Custom User-schema mappings, the same as each Production provider's
# (substructure/keycloak/Pulumi.Production.yaml): Learn gets fullName (its
# profile name) and emailOptIn (an integer on Learn's side); mitxonline gets
# neither, so locally, as in Production, a Keycloak name change does not reach
# mitxonline's name. To test mitxonline name sync, add fullName to its list.
LEARN_USER_ATTRIBUTES = [
    ScimUserAttribute(name="fullName", type="string", custom_attribute_name="fullName"),
    ScimUserAttribute(
        name="emailOptIn", type="integer", custom_attribute_name="emailOptIn"
    ),
]
MITXONLINE_USER_ATTRIBUTES: list[ScimUserAttribute] = []


def create_scim_remote_providers(  # noqa: PLR0913
    keycloak_provider: keycloak.Provider,
    keycloak_url: str,
    realm: keycloak.Realm,
    learn_scim_token: str,
    mitxonline_scim_token: str,
    mitxonline_scim_base_url: str,
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
    :param mitxonline_scim_base_url: mitxonline's in-cluster SCIM root, ending
        /scim/v2.
    :param realm_name: Name of the realm whose users are provisioned.
    :param enabled_apps: Which apps the stack is running. A provider is created
        only for an app that is on: its target Service does not exist otherwise,
        and with replay_on_failure the plugin would queue every failed push for
        retry indefinitely. Mirrors the gating on the per-app clients in
        keycloak.py.
    """
    resource_options = ResourceOptions(provider=keycloak_provider, depends_on=[realm])
    invoke_options = InvokeOptions(provider=keycloak_provider)

    # Plugin 4.x accepts a client_credentials token from a master-realm service
    # account holding scim-admin. The client only exists in master, and the
    # backend looks the token's client up in the realm from the URL, so the
    # provider always addresses /realms/master (see the provider's docstring).
    scim_client = keycloak.openid.Client(
        "master-scim-config-manager-client",
        realm_id=MASTER_REALM,
        client_id="scim-config-manager",
        name="scim-config-manager",
        description="Pulumi service account for the scim-for-keycloak admin API",
        enabled=True,
        access_type="CONFIDENTIAL",
        standard_flow_enabled=False,
        implicit_flow_enabled=False,
        direct_access_grants_enabled=False,
        service_accounts_enabled=True,
        opts=resource_options,
    )

    # scim-admin on master-realm authorises the backend at all; the per-realm
    # grant scopes which realms' providers this account may manage.
    role_grants = []
    for admin_client_id in (f"{MASTER_REALM}-realm", f"{realm_name}-realm"):
        admin_client = keycloak.openid.get_client(
            realm_id=MASTER_REALM, client_id=admin_client_id, opts=invoke_options
        )
        role_grants.append(
            keycloak.openid.ClientServiceAccountRole(
                f"master-scim-config-manager-{admin_client_id}-scim-admin",
                realm_id=MASTER_REALM,
                service_account_user_id=scim_client.service_account_user_id,
                client_id=admin_client.id,
                role=SCIM_ADMIN_ROLE,
                opts=resource_options,
            )
        )

    # Keyed by the app name the Tiltfile uses, so enabled_apps gates them.
    targets = {
        "mit-learn": (
            "ol-learn-scim",
            "http://mitlearn-webapp.mit-learn.svc.cluster.local:8060/scim/v2",
            learn_scim_token,
            LEARN_USER_ATTRIBUTES,
        ),
        "mitxonline": (
            "ol-mitxonline-scim",
            mitxonline_scim_base_url,
            mitxonline_scim_token,
            MITXONLINE_USER_ATTRIBUTES,
        ),
    }

    for app, (name, base_url, token, user_attributes) in targets.items():
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
                # Match production's providers (see Pulumi.Production.yaml), so
                # local-dev exercises the same request shapes. Without
                # patch_supported the plugin sends PUTs where production sends
                # PATCHes. Every flag is declared, off ones included, because
                # loading the default configuration resets undeclared ones to
                # RFC defaults.
                filter_supported=True,
                filter_max_results=50,
                sort_supported=True,
                patch_supported=True,
                bulk_supported=True,
                bulk_max_operations=100,
                bulk_max_payload_size=1048576,
                etag_supported=False,
                change_password_supported=False,
                authentication_list=[
                    ScimAuthentication(
                        authentication_type="BEARER",
                        authentication_token=token,
                    )
                ],
                user_attributes=user_attributes,
            ),
            opts=ResourceOptions(depends_on=[*role_grants, realm]),
        )


__all__ = ["create_scim_remote_providers"]
