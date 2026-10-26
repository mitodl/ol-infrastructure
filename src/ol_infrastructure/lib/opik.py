"""Opik settings shared by the applications that send it traces.

Every environment traces to the Production Opik, so one UI shows an application's
CI, QA, and Production traces side by side, told apart by each trace's environment.
SDK clients authenticate with ``ol-opik-sdk``, a client-credentials-only client in
Production Keycloak. The Production keycloak substructure writes its credentials to
the same path in every environment's Vault, because the Production Opik gateway only
accepts tokens Production Keycloak signed.
"""

from functools import cache

from pulumi import Output

from ol_infrastructure.lib import pulumi_projects as projects
from ol_infrastructure.lib.pulumi_helper import make_stack_reference

OPIK_STACK_NAME = "Production"
OPIK_SDK_VAULT_MOUNT = "secret-operations"
OPIK_SDK_VAULT_PATH = "sso/opik-sdk"
# The environments whose Vault receives the ol-opik-sdk credentials.
OPIK_SDK_ENVIRONMENTS = ("ci", "qa", "production")
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
