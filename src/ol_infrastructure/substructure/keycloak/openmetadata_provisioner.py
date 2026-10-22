"""Keycloak identity that Pulumi uses to manage OpenMetadata as an admin.

OpenMetadata reserves bots, their roles and their tokens for admins, and only
``initialAdmins`` (applications/open_metadata) are admins. That list names this
client's service account, so the OpenMetadata dynamic providers
(ol_infrastructure.providers.openmetadata) can get an admin token with
client_credentials instead of a long-lived admin JWT made by hand.

The client holds no Keycloak roles: its access comes from OpenMetadata alone.
"""

import json

import pulumi_keycloak as keycloak
import pulumi_vault as vault
from pulumi import Output, ResourceOptions

REALM_NAME = "ol-data-platform"
OPENMETADATA_PROVISIONER_CLIENT_ID = "ol-openmetadata-provisioner"
# Keycloak names every service account user after its client.
OPENMETADATA_PROVISIONER_USERNAME = (
    f"service-account-{OPENMETADATA_PROVISIONER_CLIENT_ID}"
)
OPENMETADATA_PROVISIONER_VAULT_MOUNT = "secret-openmetadata"
OPENMETADATA_PROVISIONER_VAULT_NAME = "pulumi-provisioner"


def create_openmetadata_provisioner_resources(
    keycloak_provider: keycloak.Provider,
    keycloak_url: str,
    realm: keycloak.Realm,
) -> None:
    """Declare the provisioner client and its Vault secret.

    :param keycloak_provider: Provider for the Keycloak instance.
    :param keycloak_url: Base URL of the Keycloak instance.
    :param realm: The ``ol-data-platform`` realm.
    """
    resource_options = ResourceOptions(provider=keycloak_provider)

    provisioner_client = keycloak.openid.Client(
        "ol-data-platform-openmetadata-provisioner-client",
        name="ol-data-platform-openmetadata-provisioner-client",
        realm_id=realm.id,
        client_id=OPENMETADATA_PROVISIONER_CLIENT_ID,
        enabled=True,
        access_type="CONFIDENTIAL",
        service_accounts_enabled=True,
        standard_flow_enabled=False,
        implicit_flow_enabled=False,
        direct_access_grants_enabled=False,
        opts=resource_options.merge(ResourceOptions(delete_before_replace=True)),
    )

    # OpenMetadata takes the principal from the first of email,
    # preferred_username and sub that the token carries. A service account has
    # no email, and preferred_username otherwise depends on the realm's default
    # client scopes, so the claim is mapped on the client itself.
    keycloak.openid.UserPropertyProtocolMapper(
        "ol-data-platform-openmetadata-provisioner-username-mapper",
        realm_id=realm.id,
        client_id=provisioner_client.id,
        name="preferred_username",
        user_property="username",
        claim_name="preferred_username",
        add_to_access_token=True,
        add_to_id_token=False,
        add_to_userinfo=False,
        opts=resource_options,
    )

    # Filed in OpenMetadata's own kv mount, next to the team sync client: the
    # secret makes its holder an OpenMetadata admin, and application policies
    # read secret-operations/*. No Kubernetes workload reads it; Pulumi does.
    vault.generic.Secret(
        "ol-data-platform-openmetadata-provisioner-vault-credentials",
        path=f"{OPENMETADATA_PROVISIONER_VAULT_MOUNT}/{OPENMETADATA_PROVISIONER_VAULT_NAME}",
        data_json=Output.all(
            token_url=(
                f"{keycloak_url}/realms/{REALM_NAME}/protocol/openid-connect/token"
            ),
            client_id=provisioner_client.client_id,
            client_secret=provisioner_client.client_secret,
        ).apply(json.dumps),
    )
