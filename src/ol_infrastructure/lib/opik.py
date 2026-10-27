"""Opik settings shared by the applications that send it traces.

Every environment traces to the Production Opik, so one UI shows an application's
CI, QA, and Production traces side by side; each application puts its environment on
its traces. Each application authenticates in each environment with a client of its
own, ``opik-<app>-<env>``, a client-credentials-only client in Production Keycloak
(the Production Opik gateway only accepts tokens Production Keycloak signed). The
Production keycloak substructure writes each one to ``secret-opik/sdk-<app>`` in that
environment's Vault, so a credential can be revoked without touching another app or
environment, and the gateway and Keycloak logs say which one wrote.

The credentials get their own KV mount rather than ``secret-operations``, which some
application policies read wholesale (e.g. odl_video_service's
``secret-operations/*``). A token from any of these clients can read every Production
trace and edit the Prompt Library, so among applications only each app's own policy
can read its path. The developer (``secret-*``) and Concourse (``*``) policies still
can, as they can ``secret-operations``.
"""

from functools import cache
from typing import Literal

from pulumi import Output

from ol_infrastructure.lib import pulumi_projects as projects
from ol_infrastructure.lib.pulumi_helper import make_stack_reference

OPIK_STACK_NAME = "Production"
OPIK_SDK_VAULT_MOUNT = "secret-opik"
# The applications that trace to Opik, named for their Opik project.
OpikApp = Literal["mit-learn", "learn-ai", "dagster-ml", "gwarek"]
# The environments each application runs in, and so gets a client for. gwarek has
# only a Production stack.
OPIK_APP_ENVIRONMENTS: dict[OpikApp, tuple[str, ...]] = {
    "mit-learn": ("ci", "qa", "production"),
    "learn-ai": ("ci", "qa", "production"),
    "dagster-ml": ("ci", "qa", "production"),
    "gwarek": ("production",),
}
# The environments whose Vault holds the secret-opik mount.
OPIK_SDK_ENVIRONMENTS = ("ci", "qa", "production")


def opik_sdk_client_id(app: OpikApp, env_suffix: str) -> str:
    """Return the Keycloak client id of an application's Opik client in an environment.

    :param app: The application.
    :param env_suffix: The environment, e.g. ``qa``.
    :returns: The client id, e.g. ``opik-learn-ai-qa``.
    :rtype: str
    """
    return f"opik-{app}-{env_suffix}"


def opik_sdk_vault_path(app: OpikApp) -> str:
    """Return the path under OPIK_SDK_VAULT_MOUNT of an application's Opik client.

    The path is the same in every environment's Vault; each holds that
    environment's client.

    :param app: The application.
    :returns: The KV v1 path, e.g. ``sdk-learn-ai``.
    :rtype: str
    """
    return f"sdk-{app}"


def opik_keycloak_vault_env(app: OpikApp) -> dict[str, str]:
    """Return the variables naming where an application's Opik client is in Vault.

    For code that reads the client from Vault itself (dagster's ml code location).

    :param app: The application.
    :returns: OPIK_KEYCLOAK_VAULT_MOUNT and OPIK_KEYCLOAK_VAULT_PATH.
    :rtype: dict[str, str]
    """
    return {
        "OPIK_KEYCLOAK_VAULT_MOUNT": OPIK_SDK_VAULT_MOUNT,
        "OPIK_KEYCLOAK_VAULT_PATH": opik_sdk_vault_path(app),
    }


# VaultStaticSecret templates for the OPIK_KEYCLOAK_* variables the SDK auth reads.
# The token endpoint is derived from the realm url stored alongside the client.
OPIK_KEYCLOAK_SECRET_TEMPLATES = {
    "OPIK_KEYCLOAK_CLIENT_ID": '{{ get .Secrets "client_id" }}',
    "OPIK_KEYCLOAK_CLIENT_SECRET": '{{ get .Secrets "client_secret" }}',
    "OPIK_KEYCLOAK_TOKEN_URL": '{{ get .Secrets "url" }}/protocol/openid-connect/token',
}


@cache
def opik_api_url() -> Output[str]:
    """Return the Production Opik API URL, in the form OPIK_URL_OVERRIDE expects.

    Cached so that a stack calling it more than once declares one StackReference.

    :returns: The ``/api/`` URL of the Production Opik.
    :rtype: Output[str]
    """
    opik_stack = make_stack_reference(projects.OPIK, OPIK_STACK_NAME)
    return opik_stack.require_output("opik_url").apply(lambda url: f"{url}/api/")
