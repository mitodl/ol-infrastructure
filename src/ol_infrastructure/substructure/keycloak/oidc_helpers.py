"""Helpers for configuring Keycloak OIDC identity providers."""

import json
from typing import Any

import httpx
import pulumi

# Discovery document keys and the keycloak.oidc.IdentityProvider arguments
# they populate.
_DISCOVERY_IDP_ARGS = {
    "authorization_endpoint": "authorization_url",
    "token_endpoint": "token_url",
    "userinfo_endpoint": "user_info_url",
    "end_session_endpoint": "logout_url",
    "jwks_uri": "jwks_url",
    "issuer": "issuer",
}


class OidcDiscoveryError(Exception):
    """An OIDC provider's discovery document could not be fetched or used.

    Raised instead of returning None so that an unusable partner endpoint, or an
    unreachable one with no saved copy, fails the deploy. Returning None dropped
    the IdP resource from the Pulumi program, which Pulumi then deleted along
    with the working integration.
    """


def _fetch_oidc_discovery(discovery_url: str) -> dict[str, Any]:
    try:
        response = httpx.get(discovery_url, timeout=10)
        # An error response with a JSON body parses fine, so without this an
        # IdP-not-found page would be read as discovery metadata. HTTPError is
        # the base of both RequestError and the HTTPStatusError this raises.
        response.raise_for_status()
        discovery = response.json()
    except (httpx.HTTPError, ValueError) as e:
        msg = f"Unable to fetch OIDC discovery document from {discovery_url}: {e}"
        raise OidcDiscoveryError(msg) from e
    # A maintenance page served as JSON with a 200 is an outage too, and has to
    # reach the saved copy rather than be deployed or crash the drift check.
    if (
        not isinstance(discovery, dict)
        or not {
            "authorization_endpoint",
            "token_endpoint",
        }
        <= discovery.keys()
    ):
        msg = (
            f"Unable to fetch OIDC discovery document from {discovery_url}: the "
            "response has no authorization_endpoint and token_endpoint"
        )
        raise OidcDiscoveryError(msg)
    return discovery


def _parse_saved_discovery(saved_json: str, discovery_url: str) -> dict[str, Any]:
    try:
        return json.loads(saved_json)
    except ValueError as e:
        msg = (
            f"Unable to parse the saved OIDC discovery document for "
            f"{discovery_url}: {e}"
        )
        raise OidcDiscoveryError(msg) from e


def load_oidc_discovery(discovery_url: str, saved_json: str | None) -> dict[str, Any]:
    """Return a provider's discovery document, from its URL when it answers.

    A saved copy keeps the stack deployable while the partner's endpoint is
    down: the fetch failure becomes a warning and the IdP is built from the
    copy. When both are available the live document wins, and a difference in
    the endpoints we deploy is logged so the copy can be refreshed.

    :param discovery_url: The full URL to the OIDC discovery endpoint.
    :param saved_json: A saved copy of the discovery document.
    :returns: The parsed discovery document.
    :rtype: dict[str, Any]
    :raises OidcDiscoveryError: The saved copy does not parse, or the fetch
        failed with no saved copy to fall back to.
    """
    try:
        live = _fetch_oidc_discovery(discovery_url)
    except OidcDiscoveryError as e:
        if saved_json is None:
            raise
        pulumi.log.warn(f"{e}. Building the IdP from the saved copy instead.")
        return _parse_saved_discovery(saved_json, discovery_url)
    if saved_json is not None:
        saved = _parse_saved_discovery(saved_json, discovery_url)
        changed = sorted(
            key for key in _DISCOVERY_IDP_ARGS if live.get(key) != saved.get(key)
        )
        if changed:
            pulumi.log.warn(
                f"OIDC discovery document at {discovery_url} differs from the saved "
                f"copy in {', '.join(changed)}. Deploying the live values; refresh "
                "the saved copy so a later outage doesn't roll them back."
            )
    return live


def oidc_identity_provider_args_from_discovery(
    oidc_provider_metadata: dict[str, Any],
    discovery_url: str,
    client_secret: str | None = None,
) -> dict[str, Any]:
    """Build a dictionary of arguments for a Keycloak OIDC Identity Provider.

    :param oidc_provider_metadata: The provider's discovery document, from
        load_oidc_discovery.
    :type oidc_provider_metadata: dict[str, Any]
    :param discovery_url: The URL the document was published at, for error
        messages.
    :type discovery_url: str
    :param client_secret: Optional client secret for client_secret_basic authentication.
        If provided, uses client_secret_basic instead of private_key_jwt.
    :type client_secret: str | None

    :return: A dictionary of arguments that can be passed to the constructor of a
        keycloak.oidc.IdentityProvider Pulumi resource.
    :rtype: Dict[str, Any]

    :raises OidcDiscoveryError: The provider does not support the scopes and
        client authentication method we require.
    """
    oidc_idp_args = {
        idp_arg: oidc_provider_metadata[key]
        for key, idp_arg in _DISCOVERY_IDP_ARGS.items()
        if key in oidc_provider_metadata
    }

    required_scopes = ("openid", "email", "profile")
    if "scopes_supported" in oidc_provider_metadata:
        supported_scopes = oidc_provider_metadata["scopes_supported"]
        missing_scopes = [
            scope for scope in required_scopes if scope not in supported_scopes
        ]
        if missing_scopes:
            msg = (
                f"OIDC provider at {discovery_url} does not support required "
                f"scopes: {', '.join(missing_scopes)}"
            )
            raise OidcDiscoveryError(msg)
    oidc_idp_args["default_scopes"] = " ".join(required_scopes)

    if client_secret:
        if (
            "token_endpoint_auth_methods_supported" in oidc_provider_metadata
            and "client_secret_basic"
            not in oidc_provider_metadata["token_endpoint_auth_methods_supported"]
        ):
            msg = (
                f"OIDC provider at {discovery_url} does not support the "
                "client_secret_basic client auth method"
            )
            raise OidcDiscoveryError(msg)
        oidc_idp_args["client_secret"] = client_secret
        oidc_idp_args["extra_config"] = {"clientAuthMethod": "client_secret_basic"}
    else:
        if (
            "token_endpoint_auth_methods_supported" not in oidc_provider_metadata
            or "private_key_jwt"
            not in oidc_provider_metadata["token_endpoint_auth_methods_supported"]
        ):
            msg = (
                f"OIDC provider at {discovery_url} does not support the "
                "private_key_jwt client auth method"
            )
            raise OidcDiscoveryError(msg)
        # pulumi-keycloak >=6.12.0 marks client_secret/client_secret_wo as
        # required even when Keycloak is configured for private_key_jwt client
        # authentication. Provide an explicit empty value to satisfy provider
        # schema validation without configuring shared-secret authentication.
        oidc_idp_args["client_secret"] = ""
        oidc_idp_args["extra_config"] = {"clientAuthMethod": "private_key_jwt"}
    return oidc_idp_args
