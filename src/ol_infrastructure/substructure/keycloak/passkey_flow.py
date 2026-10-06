"""Keycloak browser flow for the staff realms, whose login form takes a passkey."""

import pulumi
import pulumi_keycloak as keycloak


def create_passkey_browser_flow(
    realm_id: str | pulumi.Output[str],
    name_prefix: str,
    opts: pulumi.ResourceOptions | None = None,
) -> keycloak.authentication.Flow:
    """Create a browser flow whose login form is username then passkey, no password.

    An identity provider in the realm is still offered on the username form and
    reachable with ``kc_idp_hint``; a brokered login does not go through the passkey
    step.

    Every top-level step is ALTERNATIVE. Keycloak ignores the ALTERNATIVE steps of a
    flow level that also has a REQUIRED one, so a REQUIRED passkey subflow here stops
    the cookie step from running and every login prompts despite a live SSO session.

    There is no organization step. Both staff realms hold organizations, and with one
    present the organization authenticator shows its own username form ahead of the
    passkey form.

    :param realm_id: The realm the flow belongs to.
    :param name_prefix: Prefix for the resource names and flow aliases, e.g.
        ``ol-browser-data-platform``. The realm's flow binding points at the alias, so
        changing the prefix of a deployed realm replaces the bound flow.
    :param opts: Resource options applied to every resource in the flow.
    :returns: The top-level flow, to bind as the realm's browser flow.
    :rtype: keycloak.authentication.Flow
    """
    browser_flow = keycloak.authentication.Flow(
        f"{name_prefix}-flow",
        realm_id=realm_id,
        alias=f"{name_prefix}-flow",
        opts=opts,
    )
    keycloak.authentication.Execution(
        f"{name_prefix}-auth-cookie",
        realm_id=realm_id,
        parent_flow_alias=browser_flow.alias,
        authenticator="auth-cookie",
        requirement="ALTERNATIVE",
        priority=10,
        opts=opts,
    )
    keycloak.authentication.Execution(
        f"{name_prefix}-idp-redirector",
        realm_id=realm_id,
        parent_flow_alias=browser_flow.alias,
        authenticator="identity-provider-redirector",
        requirement="ALTERNATIVE",
        priority=20,
        opts=opts,
    )
    passkey_flow = keycloak.authentication.Subflow(
        f"{name_prefix}-passkey-flow",
        realm_id=realm_id,
        alias=f"{name_prefix}-passkey-flow",
        parent_flow_alias=browser_flow.alias,
        provider_id="basic-flow",
        priority=60,
        requirement="ALTERNATIVE",
        opts=opts,
    )
    keycloak.authentication.Execution(
        f"{name_prefix}-flow-username-form",
        realm_id=realm_id,
        parent_flow_alias=passkey_flow.alias,
        authenticator="auth-username-form",
        requirement="REQUIRED",
        priority=70,
        opts=opts,
    )
    keycloak.authentication.Execution(
        f"{name_prefix}-webauthn-authenticator-flow",
        realm_id=realm_id,
        parent_flow_alias=passkey_flow.alias,
        authenticator="webauthn-authenticator-passwordless",
        requirement="REQUIRED",
        priority=80,
        opts=opts,
    )
    return browser_flow
