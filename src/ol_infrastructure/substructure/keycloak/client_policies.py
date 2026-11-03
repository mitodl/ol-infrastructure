"""Realm client policies that stop a conforming client from regressing."""

import json
from typing import Any

import pulumi
import pulumi_keycloak as keycloak

PKCE_MARKER_ATTRIBUTE = "ol.pkce"
PKCE_MARKER_VALUE = "required"
MANAGED_MARKER_ATTRIBUTE = "ol.managed"
MANAGED_MARKER_VALUE = "pulumi"

# Spread into the arguments of every OpenID client Pulumi declares. The marker and the
# two flags travel together because a marked client with either grant on fails its
# next update with ``invalid_client_metadata``. The flags are not repeated at the call
# site; Python rejects the duplicate keyword.
MANAGED_CLIENT_ARGS: dict[str, Any] = {
    "implicit_flow_enabled": False,
    "direct_access_grants_enabled": False,
    "extra_config": {MANAGED_MARKER_ATTRIBUTE: MANAGED_MARKER_VALUE},
}

# Spread into a client's arguments in place of ``MANAGED_CLIENT_ARGS``. The PKCE marker
# and the method travel together because a marked client without S256 fails its next
# update with ``invalid_client_metadata``.
#
# Taking a client back out takes two deploys. Keycloak matches the policy against the
# stored client and validates the proposed one, so an update that drops both at once
# is still matched and is rejected. Drop the marker first (pass the method on its
# own), deploy, then drop the method. The same holds for the managed marker.
PKCE_REQUIRED_CLIENT_ARGS: dict[str, Any] = {
    **MANAGED_CLIENT_ARGS,
    "pkce_code_challenge_method": "S256",
    "extra_config": {
        **MANAGED_CLIENT_ARGS["extra_config"],
        PKCE_MARKER_ATTRIBUTE: PKCE_MARKER_VALUE,
    },
}


def _marker_condition(
    attribute: str, value: str
) -> keycloak.RealmClientPolicyProfilePolicyConditionArgs:
    """Match clients carrying ``attribute=value``.

    The condition is a marker and not the client's access type. A condition on
    public clients, or on every client, also matches the built-in ``account`` and
    ``admin-cli`` clients and the SAML clients, which do not conform, and the next
    update of any of them would be rejected.
    """
    return keycloak.RealmClientPolicyProfilePolicyConditionArgs(
        name="client-attributes",
        # Keycloak reads this value as a JSON string, not as a JSON list.
        configuration={"attributes": json.dumps([{"key": attribute, "value": value}])},
    )


def create_pkce_client_policy(
    realm_id: str | pulumi.Output[str],
    name_prefix: str,
    opts: pulumi.ResourceOptions | None = None,
) -> keycloak.RealmClientPolicyProfilePolicy:
    """Require PKCE with S256 of every client carrying the PKCE marker attribute.

    Keycloak runs the policy on Admin API writes as well as on authorization and
    token requests, so a marked client cannot lose PKCE through a Pulumi edit or in
    the admin console.

    ``auto-configure`` is off. With it on, Keycloak rewrites the client during the
    update instead of rejecting it, and the stored client then disagrees with the
    Pulumi inputs.

    :param realm_id: The realm the policy belongs to.
    :param name_prefix: Prefix for the Pulumi resource names, e.g. ``ol-data-platform``.
    :param opts: Resource options applied to the profile and the policy.
    :returns: The policy that applies the PKCE profile to marked clients.
    :rtype: keycloak.RealmClientPolicyProfilePolicy
    """
    profile = keycloak.RealmClientPolicyProfile(
        f"{name_prefix}-pkce-client-profile",
        realm_id=realm_id,
        name="ol-pkce",
        description="Require PKCE with the S256 challenge method.",
        executors=[
            keycloak.RealmClientPolicyProfileExecutorArgs(
                name="pkce-enforcer",
                configuration={"auto-configure": "false"},
            )
        ],
        opts=opts,
    )
    return keycloak.RealmClientPolicyProfilePolicy(
        f"{name_prefix}-pkce-client-policy",
        realm_id=realm_id,
        name="ol-pkce-clients",
        description=(
            f"Clients with the {PKCE_MARKER_ATTRIBUTE}={PKCE_MARKER_VALUE} attribute."
        ),
        enabled=True,
        profiles=[profile.name],
        conditions=[_marker_condition(PKCE_MARKER_ATTRIBUTE, PKCE_MARKER_VALUE)],
        opts=opts,
    )


def create_managed_client_policy(
    realm_id: str | pulumi.Output[str],
    name_prefix: str,
    opts: pulumi.ResourceOptions | None = None,
) -> keycloak.RealmClientPolicyProfilePolicy:
    """Reject the implicit and password grants for every client carrying the managed
    marker attribute.

    On an Admin API write of a marked client, Keycloak rejects the client if either
    flag is on. On authorization and token requests, it rejects implicit, hybrid and
    password grants whatever the client's flags say. The flags are already off on
    every marked client, so only the write check changes anything today.

    ``auto-configure`` is off, for the same reason as the PKCE profile.

    :param realm_id: The realm the policy belongs to.
    :param name_prefix: Prefix for the Pulumi resource names, e.g. ``ol-data-platform``.
    :param opts: Resource options applied to the profile and the policy.
    :returns: The policy that applies the grants profile to marked clients.
    :rtype: keycloak.RealmClientPolicyProfilePolicy
    """
    profile = keycloak.RealmClientPolicyProfile(
        f"{name_prefix}-grants-client-profile",
        realm_id=realm_id,
        name="ol-grants",
        description="Reject the implicit, hybrid and password grants.",
        executors=[
            keycloak.RealmClientPolicyProfileExecutorArgs(
                name=executor,
                configuration={"auto-configure": "false"},
            )
            for executor in ("reject-implicit-grant", "reject-ropc-grant")
        ],
        opts=opts,
    )
    return keycloak.RealmClientPolicyProfilePolicy(
        f"{name_prefix}-managed-client-policy",
        realm_id=realm_id,
        name="ol-managed-clients",
        description=(
            f"Clients with the {MANAGED_MARKER_ATTRIBUTE}={MANAGED_MARKER_VALUE} "
            "attribute."
        ),
        enabled=True,
        profiles=[profile.name],
        conditions=[_marker_condition(MANAGED_MARKER_ATTRIBUTE, MANAGED_MARKER_VALUE)],
        opts=opts,
    )


def create_client_policies(
    realm_id: str | pulumi.Output[str],
    name_prefix: str,
    opts: pulumi.ResourceOptions | None = None,
) -> None:
    """Create the PKCE and managed-client policies for a realm.

    :param realm_id: The realm the policies belong to.
    :param name_prefix: Prefix for the Pulumi resource names, e.g. ``ol-data-platform``.
    :param opts: Resource options applied to every profile and policy.
    """
    create_pkce_client_policy(realm_id, name_prefix, opts)
    create_managed_client_policy(realm_id, name_prefix, opts)
