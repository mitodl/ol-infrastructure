"""scim-for-keycloak plugin configuration.

The plugin's remote SCIM providers (outbound user provisioning to MIT Learn,
MITx Online, etc.) live in the plugin's own tables and are managed through its
SCIM admin backend by the ``ScimRemoteProvider`` dynamic resource.

Remote providers are declared per stack under ``keycloak_scim:remote_providers``:

.. code-block:: yaml

    keycloak_scim:remote_providers:
      - realm: olapps
        name: mit-learn
        base_url: https://api.learn.mit.edu/scim/v2
        enabled: true
        replay_on_failure: true
        retry_interval_in_seconds: 3600
        authentication_list:
          - authentication_type: ...
            authentication_token:
              secure: ...
"""

import json
from typing import Any

import pulumi_keycloak as keycloak
import pulumi_vault as vault
from pulumi import Config, InvokeOptions, Output, ResourceOptions

from ol_infrastructure.providers.keycloak_scim import (
    ScimRemoteProvider,
    ScimRemoteProviderSpec,
)

SCIM_ADMIN_ROLE = "scim-admin"
# The realm whose remote providers exist today; granted even before any are
# declared so the export script can read the live config with this client.
DEFAULT_SCIM_REALMS = frozenset({"olapps"})


class RealmScimRemoteProvider(ScimRemoteProviderSpec):
    """A remote SCIM provider declaration from stack config."""

    realm: str

    def to_scim(self) -> dict[str, Any]:
        return self.model_dump(by_alias=True, exclude_none=True, exclude={"realm"})


def create_scim_resources(
    keycloak_provider: keycloak.Provider,
    keycloak_url: str,
) -> None:
    """Create the SCIM admin service account and the declared remote providers."""
    resource_options = ResourceOptions(provider=keycloak_provider)
    invoke_options = InvokeOptions(provider=keycloak_provider)
    remote_providers = [
        RealmScimRemoteProvider.model_validate(entry)
        for entry in Config("keycloak_scim").get_object("remote_providers") or []
    ]

    scim_client = keycloak.openid.Client(
        "master-scim-config-manager-client",
        realm_id="master",
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

    # The plugin only accepts a service account token when it holds scim-admin
    # on the master realm's own admin client. The per-realm grants scope the
    # backend's realm authorization to the realms we actually configure.
    scim_realms = sorted(
        DEFAULT_SCIM_REALMS | {provider.realm for provider in remote_providers}
    )
    role_grants = []
    for admin_client_id in ["master-realm", *(f"{r}-realm" for r in scim_realms)]:
        admin_client = keycloak.openid.get_client(
            realm_id="master", client_id=admin_client_id, opts=invoke_options
        )
        role_grants.append(
            keycloak.openid.ClientServiceAccountRole(
                f"master-scim-config-manager-{admin_client_id}-scim-admin",
                realm_id="master",
                service_account_user_id=scim_client.service_account_user_id,
                client_id=admin_client.id,
                role=SCIM_ADMIN_ROLE,
                opts=resource_options,
            )
        )

    vault.generic.Secret(
        "master-scim-config-manager-vault-credentials",
        path="secret-operations/keycloak/scim-config-manager",
        data_json=Output.all(
            url=keycloak_url,
            auth_realm="master",
            client_id=scim_client.client_id,
            client_secret=scim_client.client_secret,
        ).apply(json.dumps),
    )

    for provider in remote_providers:
        ScimRemoteProvider(
            f"{provider.realm}-scim-remote-provider-{provider.name}",
            keycloak_url=keycloak_url,
            realm=provider.realm,
            client_id=scim_client.client_id,
            client_secret=scim_client.client_secret,
            spec=provider,
            opts=ResourceOptions(depends_on=role_grants),
        )
