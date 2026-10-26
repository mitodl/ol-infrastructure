"""Opik settings shared by the applications that send it traces.

Every environment traces to the Production Opik, so one UI shows an application's
CI, QA, and Production traces side by side; each application puts its environment on
its traces. SDK clients authenticate with ``ol-opik-sdk``, a client-credentials-only
client in Production Keycloak. The Production keycloak substructure writes its
credentials to the same path in every environment's Vault, because the Production
Opik gateway only accepts tokens Production Keycloak signed.

The credentials get their own KV mount rather than ``secret-operations``, which some
application policies read wholesale (e.g. odl_video_service's
``secret-operations/*``). A token from this client can read every Production trace
and edit the Prompt Library, so among applications only the policies that name this
path can read it. The developer (``secret-*``) and Concourse (``*``) policies still
can, as they can ``secret-operations``.
"""

from functools import cache

from pulumi import Output

from ol_infrastructure.lib import pulumi_projects as projects
from ol_infrastructure.lib.pulumi_helper import make_stack_reference

OPIK_STACK_NAME = "Production"
OPIK_SDK_VAULT_MOUNT = "secret-opik"
OPIK_SDK_VAULT_PATH = "sdk-client"
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
