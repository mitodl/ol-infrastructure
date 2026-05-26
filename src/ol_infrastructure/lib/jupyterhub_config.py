"""Shared JupyterHub configuration helpers.

These utilities are consumed by multiple Pulumi application stacks (e.g.
``applications/jupyterhub`` and ``applications/jupyterhub_data``).  Placing
them here avoids cross-project imports, which is an anti-pattern in this
monorepo.
"""

from typing import Any


class InvalidAuthenticatorError(Exception):
    """Raised when an unsupported JupyterHub authenticator class is requested."""

    def __init__(self, authenticator_type: str) -> None:
        """Initialise with the unrecognised authenticator type name."""
        super().__init__(f"Invalid authenticator type: {authenticator_type}")


# Authenticators all have slightly different semantics for which traitlets
# are used from where. This function needs to spit out the entire block.
# At the moment this spits out the hub.config block,
# which we only use for authenticator configuration.
def get_authenticator_config(
    jupyterhub_deployment_config: dict[str, Any],
) -> dict[str, Any]:
    """Return the ``hub.config`` block for the given authenticator type.

    :param jupyterhub_deployment_config: Flat dict of deployment configuration
        values.  The following keys are recognised:

        * ``authenticator_class`` — one of ``"shared-password"``, ``"tmp"``,
          or ``"generic-oauth"`` (default ``"tmp"``).
        * ``admin_users`` — list of admin usernames (default ``[]``).
        * ``allowed_users`` — list of allowed usernames (default ``[]``).
        * ``shared_password`` — required when ``authenticator_class`` is
          ``"shared-password"``.
        * ``keycloak_base_url`` — base URL for Keycloak (default
          ``"https://sso.ol.mit.edu"``); used with ``"generic-oauth"``.
        * ``keycloak_realm`` — Keycloak realm name (default
          ``"ol-data-platform"``); used with ``"generic-oauth"``.
        * ``login_service`` — display name shown on the login button (default
          ``"MIT OL Data Platform"``); used with ``"generic-oauth"``.
        * ``username_claim`` — JWT claim mapped to the JupyterHub username
          (default ``"preferred_username"``); used with ``"generic-oauth"``.

    :returns: Dict suitable for use as the ``hub.config`` Helm value.
    :raises InvalidAuthenticatorError: If ``authenticator_class`` is not one
        of the recognised values.
    """
    authenticator_type = (
        jupyterhub_deployment_config.get("authenticator_class") or "tmp"
    )
    if authenticator_type not in ["shared-password", "tmp", "generic-oauth"]:
        raise InvalidAuthenticatorError(authenticator_type)

    admin_users_list = jupyterhub_deployment_config.get("admin_users") or []
    allowed_users_list = jupyterhub_deployment_config.get("allowed_users") or []
    auth_conf: dict[str, Any] = {
        "Authenticator": {
            "admin_users": admin_users_list,
            "allowed_users": allowed_users_list,
        }
    }
    if authenticator_type == "shared-password":
        auth_conf["SharedPasswordAuthenticator"] = {
            "admin_password": jupyterhub_deployment_config.get("shared_password")
        }
        auth_conf["JupyterHub"] = {
            "authenticator_class": "shared-password",
        }
    elif authenticator_type == "tmp":
        auth_conf["JupyterHub"] = {"authenticator_class": "tmp"}
    elif authenticator_type == "generic-oauth":
        keycloak_base_url = (
            jupyterhub_deployment_config.get("keycloak_base_url")
            or "https://sso.ol.mit.edu"
        )
        realm = jupyterhub_deployment_config.get("keycloak_realm") or "ol-data-platform"
        auth_conf["JupyterHub"] = {"authenticator_class": "generic-oauth"}
        auth_conf["GenericOAuthenticator"] = {
            "authorize_url": (
                f"{keycloak_base_url}/realms/{realm}/protocol/openid-connect/auth"
            ),
            "token_url": (
                f"{keycloak_base_url}/realms/{realm}/protocol/openid-connect/token"
            ),
            "userdata_url": (
                f"{keycloak_base_url}/realms/{realm}/protocol/openid-connect/userinfo"
            ),
            "login_service": (
                jupyterhub_deployment_config.get("login_service")
                or "MIT OL Data Platform"
            ),
            "username_claim": (
                jupyterhub_deployment_config.get("username_claim")
                or "preferred_username"
            ),
            "scope": ["openid", "profile", "email", "ol_roles"],
            "enable_auth_state": True,
        }
    return auth_conf
