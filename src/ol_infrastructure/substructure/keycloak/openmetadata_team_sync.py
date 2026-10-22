"""Keycloak side of the OpenMetadata team membership sync.

The om-ingest-team-keycloak-sync CronOMJob (substructure/open_metadata) mirrors
each OpenMetadata team into a child of the group declared here. The children
and their members are written by that job, not by Pulumi.

These groups are separate from the client roles on ``ol-open_metadata-client``,
which feed OpenMetadata's ``roles`` claim. Nothing here maps a role to a group,
so membership grants no access until a consumer is wired to it.
"""

import json

import pulumi_keycloak as keycloak
import pulumi_vault as vault
from pulumi import InvokeOptions, Output, ResourceOptions

REALM_NAME = "ol-data-platform"


def create_openmetadata_team_sync_resources(
    keycloak_provider: keycloak.Provider,
    keycloak_url: str,
    realm: keycloak.Realm,
) -> None:
    """Declare the parent group, the sync job's client, and its Vault secret.

    :param keycloak_provider: Provider for the Keycloak instance.
    :param keycloak_url: Base URL of the Keycloak instance.
    :param realm: The ``ol-data-platform`` realm.
    """
    resource_options = ResourceOptions(provider=keycloak_provider)

    parent_group = keycloak.Group(
        "ol-data-platform-openmetadata-teams-group",
        realm_id=realm.id,
        name="openmetadata-teams",
        opts=resource_options,
    )

    # Client-credentials only: no human logs in as this, so there is no redirect
    # URI and a leaked secret buys only what the role below grants.
    sync_client = keycloak.openid.Client(
        "ol-data-platform-openmetadata-team-sync-client",
        name="ol-data-platform-openmetadata-team-sync-client",
        realm_id=realm.id,
        client_id="ol-openmetadata-team-sync",
        enabled=True,
        access_type="CONFIDENTIAL",
        service_accounts_enabled=True,
        standard_flow_enabled=False,
        implicit_flow_enabled=False,
        direct_access_grants_enabled=False,
        opts=resource_options.merge(ResourceOptions(delete_before_replace=True)),
    )

    # manage-users is the one realm-management role that allows creating
    # groups and changing membership, and in Keycloak 26 it also covers
    # viewing users and groups, so view-users and query-groups would add
    # nothing. It is realm-wide: it can edit any user in the realm.
    realm_management_client = keycloak.openid.get_client(
        realm_id=REALM_NAME,
        client_id="realm-management",
        opts=InvokeOptions(provider=keycloak_provider),
    )
    keycloak.openid.ClientServiceAccountRole(
        "ol-openmetadata-team-sync-service-account-manage-users",
        realm_id=realm.id,
        service_account_user_id=sync_client.service_account_user_id,
        client_id=realm_management_client.id,
        role="manage-users",
        opts=resource_options,
    )

    # Read by the open_metadata application stack into the om-team-keycloak-sync
    # Kubernetes secret. Filed in OpenMetadata's own kv mount, which that stack
    # creates, rather than secret-operations/sso/ with the other clients:
    # application policies read secret-operations/* (e.g. odl_video_service),
    # and this secret can edit any user in the realm.
    vault.generic.Secret(
        "ol-data-platform-openmetadata-team-sync-vault-credentials",
        path="secret-openmetadata/keycloak-team-sync",
        data_json=Output.all(
            url=f"{keycloak_url}/realms/{REALM_NAME}",
            admin_url=f"{keycloak_url}/admin/realms/{REALM_NAME}",
            client_id=sync_client.client_id,
            client_secret=sync_client.client_secret,
            parent_group_id=parent_group.id,
        ).apply(json.dumps),
    )
