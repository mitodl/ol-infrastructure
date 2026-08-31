"""Pod wiring for Azure workload identity federation.

`azure-identity` exchanges a projected ServiceAccount token for an Entra access token,
so a workload reaching Azure OpenAI needs a token volume and four environment
variables and nothing else. There is no credential to store, rotate, or restart pods
for, and no Vault object involved.

The audience on the projected token is deliberately not IRSA's `sts.amazonaws.com`.
The two projections are independent, so adding this one does not disturb existing IRSA
behaviour on the same ServiceAccount.

On AKS the `azure-workload-identity` mutating webhook injects all of this. We write it
directly instead: it is these few lines against a cluster-wide webhook, another Helm
release, and another upgrade surface.

The ServiceAccount a workload runs under has to match the `subject` of the federated
identity credential in `infrastructure/azure/openai` exactly -- there is no wildcard
support anywhere in a federated credential, and a mismatch creates successfully and
fails only later at token exchange with `AADSTS70021`.
"""

from typing import Any

import pulumi_kubernetes as kubernetes
from pulumi import StackReference

TOKEN_VOLUME_NAME = "azure-identity-token"  # noqa: S105
TOKEN_MOUNT_DIR = "/var/run/secrets/azure/tokens"  # noqa: S105
TOKEN_EXPIRATION_SECONDS = 60 * 60
# Azure rejects a federated credential with any other audience count:
# "Federated identity credentials must have exactly one audience".
TOKEN_EXCHANGE_AUDIENCE = "api://AzureADTokenExchange"  # noqa: S105
AUTHORITY_HOST = "https://login.microsoftonline.com/"


def azure_identity_token_volume() -> kubernetes.core.v1.VolumeArgs:
    """Projected ServiceAccount token that azure-identity exchanges for an Entra token."""  # noqa: E501
    return kubernetes.core.v1.VolumeArgs(
        name=TOKEN_VOLUME_NAME,
        projected=kubernetes.core.v1.ProjectedVolumeSourceArgs(
            sources=[
                kubernetes.core.v1.VolumeProjectionArgs(
                    service_account_token=kubernetes.core.v1.ServiceAccountTokenProjectionArgs(
                        path=TOKEN_VOLUME_NAME,
                        expiration_seconds=TOKEN_EXPIRATION_SECONDS,
                        audience=TOKEN_EXCHANGE_AUDIENCE,
                    )
                )
            ]
        ),
    )


def azure_identity_token_mount() -> kubernetes.core.v1.VolumeMountArgs:
    """Mount for the volume from :func:`azure_identity_token_volume`."""
    return kubernetes.core.v1.VolumeMountArgs(
        name=TOKEN_VOLUME_NAME,
        mount_path=TOKEN_MOUNT_DIR,
        read_only=True,
    )


def azure_identity_env(
    azure_openai_stack: StackReference,
    consumer: str,
) -> dict[str, Any]:
    """Environment WorkloadIdentityCredential reads out of the process environment.

    :param azure_openai_stack: StackReference on ``infrastructure/azure/openai``.
    :param consumer: Key into that stack's ``workload_identities`` output --
        ``mitlearn``, ``learn-ai``, or ``mitxonline``.
    """
    return {
        "AZURE_CLIENT_ID": azure_openai_stack.require_output(
            "workload_identities"
        ).apply(lambda identities: identities[consumer]["client_id"]),
        "AZURE_TENANT_ID": azure_openai_stack.require_output("tenant_id"),
        "AZURE_FEDERATED_TOKEN_FILE": f"{TOKEN_MOUNT_DIR}/{TOKEN_VOLUME_NAME}",
        "AZURE_AUTHORITY_HOST": AUTHORITY_HOST,
    }


def azure_openai_env(
    azure_openai_stack: StackReference,
    consumer: str,
    *,
    api_version: str,
    default_deployment: str,
) -> dict[str, Any]:
    """Non-secret Azure OpenAI endpoint settings for a consumer's containers."""
    return {
        "AZURE_OPENAI_ENDPOINT": azure_openai_stack.require_output(
            "cognitive_accounts"
        ).apply(lambda accounts: accounts[consumer]["endpoint"]),
        "AZURE_OPENAI_API_VERSION": api_version,
        "AZURE_OPENAI_DEFAULT_DEPLOYMENT": default_deployment,
    }
