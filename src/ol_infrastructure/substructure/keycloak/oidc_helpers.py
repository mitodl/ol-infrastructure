from typing import Any

import httpx


def oidc_identity_provider_args_from_discovery_url(
    discovery_url: str,
) -> dict[str, Any]:
    """Build a dictionary of arguments for a Keycloak OIDC Identity Provider.

    This helper function is used to simplify the process of registering an OIDC identity
    provider in Keycloak by fetching the provider's metadata from its discovery URL.

    :param discovery_url: The full URL to the OIDC discovery endpoint
        (e.g. https://accounts.google.com/.well-known/openid-configuration)
    :type discovery_url: str

    :raises RuntimeError: If there is an error fetching or parsing the discovery
        document.

    :return: A dictionary of arguments that can be passed to the constructor of a
        keycloak.oidc.IdentityProvider Pulumi resource.
    :rtype: Dict[str, Any]
    """
    try:
        oidc_provider_metadata = httpx.get(discovery_url, timeout=10).json()
    except (httpx.RequestError, ValueError) as e:
        msg = f"Unable to fetch and parse OIDC discovery document from {discovery_url}"
        raise RuntimeError(msg) from e
    keycloak_arg_map = {
        "authorization_endpoint": "authorization_url",
        "token_endpoint": "token_url",
        "userinfo_endpoint": "user_info_url",
        "end_session_endpoint": "logout_url",
        "jwks_uri": "jwks_url",
    }
    oidc_idp_args = {
        keycloak_arg_map[key]: oidc_provider_metadata[key]
        for key in keycloak_arg_map
        if key in oidc_provider_metadata
    }
    if "issuer" in oidc_provider_metadata:
        oidc_idp_args["issuer"] = oidc_provider_metadata["issuer"]

    required_scopes = ("openid", "email", "profile")
    if "scopes_supported" in oidc_provider_metadata:
        supported_scopes = oidc_provider_metadata["scopes_supported"]
        missing_scopes = [
            scope for scope in required_scopes if scope not in supported_scopes
        ]
        if missing_scopes:
            msg = f"OIDC provider at {discovery_url} does not support required scopes: {', '.join(missing_scopes)}"  # noqa: E501
            raise RuntimeError(msg)
        oidc_idp_args["default_scopes"] = " ".join(required_scopes)
    else:
        oidc_idp_args["default_scopes"] = " ".join(required_scopes)
    if (
        "token_endpoint_auth_methods_supported" not in oidc_provider_metadata
        or "private_key_jwt"
        not in oidc_provider_metadata["token_endpoint_auth_methods_supported"]
    ):
        msg = f"OIDC provider at {discovery_url} does not support private_key_jwt client auth method"  # noqa: E501
        raise RuntimeError(msg)
    oidc_idp_args["extra_config"] = {"clientAuthMethod": "private_key_jwt"}
    return oidc_idp_args
