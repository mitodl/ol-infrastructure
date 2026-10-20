"""Realm client policies that stop a conforming client from regressing."""

import json
from typing import Any

import pulumi
import pulumi_keycloak as keycloak

PKCE_MARKER_ATTRIBUTE = "ol.pkce"
PKCE_MARKER_VALUE = "required"

# Spread into a client's arguments. The marker and the method travel together because
# a marked client without S256 fails its next update with ``invalid_client_metadata``.
#
# Taking a client back out takes two deploys. Keycloak matches the policy against the
# stored client and validates the proposed one, so an update that drops both at once
# is still matched and is rejected. Drop the marker first (pass the method on its
# own), deploy, then drop the method.
PKCE_REQUIRED_CLIENT_ARGS: dict[str, Any] = {
    "pkce_code_challenge_method": "S256",
    "extra_config": {PKCE_MARKER_ATTRIBUTE: PKCE_MARKER_VALUE},
}


def create_pkce_client_policy(
    realm_id: str | pulumi.Output[str],
    name_prefix: str,
    opts: pulumi.ResourceOptions | None = None,
) -> keycloak.RealmClientPolicyProfilePolicy:
    """Require PKCE with S256 of every client carrying the PKCE marker attribute.

    Keycloak runs the policy on Admin API writes as well as on authorization and
    token requests, so a marked client cannot lose PKCE through a Pulumi edit or in
    the admin console.

    The condition is the marker and not the client's access type. A condition on
    public clients also matches the built-in ``account`` client and the SAML clients,
    which cannot carry the PKCE attribute, and the next update of any of them would
    be rejected.

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
        conditions=[
            keycloak.RealmClientPolicyProfilePolicyConditionArgs(
                name="client-attributes",
                # Keycloak reads this value as a JSON string, not as a JSON list.
                configuration={
                    "attributes": json.dumps(
                        [{"key": PKCE_MARKER_ATTRIBUTE, "value": PKCE_MARKER_VALUE}]
                    )
                },
            )
        ],
        opts=opts,
    )
