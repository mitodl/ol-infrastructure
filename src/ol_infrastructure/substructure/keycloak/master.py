"""Keycloak master realm resource definitions.

Resources that live in the master realm but are not realm-specific (e.g. the
scim-manager service account used to manage scim-for-keycloak via its admin
REST API).
"""

import json

import pulumi_keycloak as keycloak
import pulumi_vault as vault
from pulumi import Config, InvokeOptions, Output, ResourceOptions


def create_master_realm_resources(
    keycloak_provider: keycloak.Provider,
    keycloak_url: str,
) -> None:
    """Create master-realm-scoped resources shared across all Keycloak realms."""
    resource_options = ResourceOptions(provider=keycloak_provider)
    keycloak_realm_config = Config("keycloak_realm")

    # ── scim-manager service account ────────────────────────────────────────
    # scim-for-keycloak enterprise plugin's admin backend requires a confidential
    # client with service accounts; it explicitly rejects admin-cli (public client)
    # tokens and mandates the client_credentials grant.
    scim_manager_client = keycloak.openid.Client(
        "master-scim-manager-client",
        name="scim-manager",
        realm_id="master",
        client_id="scim-manager",
        description="Service account for scim-for-keycloak admin API management",
        enabled=True,
        access_type="CONFIDENTIAL",
        standard_flow_enabled=False,
        implicit_flow_enabled=False,
        direct_access_grants_enabled=False,
        service_accounts_enabled=True,
        valid_redirect_uris=[],
        opts=resource_options.merge(ResourceOptions(delete_before_replace=True)),
    )

    # Grant realm-management and scim-admin roles to the service account.
    #
    # All get_client() invokes below require a live, initialized Keycloak:
    #   - realm-management: built-in Keycloak client, exists once the master realm
    #     is set up — but absent in local/CI environments with a fresh Keycloak.
    #   - {realm}-realm: Keycloak creates these when realms are provisioned.
    #   - scim-admin role: created by scim-for-keycloak at plugin init time.
    #
    # Gate all lookups on scim-plugin-managed-realms (default: []). Populate
    # this per-stack once Keycloak and the plugin are both running in that env.
    #
    # Example production config:
    #   keycloak_realm:scim-plugin-managed-realms:
    #     - master
    #     - olapps
    #     - ol-mit
    #     - ol-data-platform
    #     - ol-platform-engineering
    scim_managed_realms: list[str] = (
        keycloak_realm_config.get_object("scim-plugin-managed-realms") or []
    )

    if scim_managed_realms:
        master_realm_mgmt = keycloak.openid.get_client(
            realm_id="master",
            client_id="realm-management",
            opts=InvokeOptions(provider=keycloak_provider),
        )
        for resource_name, role_name in [
            ("master-scim-manager-manage-realm", "manage-realm"),
            ("master-scim-manager-manage-users", "manage-users"),
            ("master-scim-manager-manage-clients", "manage-clients"),
            ("master-scim-manager-view-realm", "view-realm"),
            ("master-scim-manager-view-users", "view-users"),
            ("master-scim-manager-query-users", "query-users"),
            ("master-scim-manager-query-realms", "query-realms"),
        ]:
            keycloak.openid.ClientServiceAccountRole(
                resource_name,
                realm_id="master",
                service_account_user_id=scim_manager_client.service_account_user_id,
                client_id=master_realm_mgmt.id,
                role=role_name,
                opts=resource_options,
            )

    for realm_name in scim_managed_realms:
        realm_mgmt_client = keycloak.openid.get_client(
            realm_id="master",
            client_id=f"{realm_name}-realm",
            opts=InvokeOptions(provider=keycloak_provider),
        )
        keycloak.openid.ClientServiceAccountRole(
            f"master-scim-manager-scim-admin-{realm_name}",
            realm_id="master",
            service_account_user_id=scim_manager_client.service_account_user_id,
            client_id=realm_mgmt_client.id,
            role="scim-admin",
            opts=resource_options,
        )

    vault.generic.Secret(
        "master-scim-manager-vault-credentials",
        path="secret-operations/keycloak/scim-manager",
        data_json=Output.all(
            client_id=scim_manager_client.client_id,
            client_secret=scim_manager_client.client_secret,
            url=keycloak_url,
            auth_realm="master",
        ).apply(json.dumps),
    )
