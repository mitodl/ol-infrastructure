"""Credential resolution and provider construction for GCP Pulumi stacks.

Every GCP stack in this repository builds its provider through
:func:`gcp_provider` rather than relying on ambient application-default
credentials.  Pulumi runs from Concourse workers and from laptops that are
routinely authenticated to *someone's* Google account; picking up whatever
identity happens to be in the environment is how the legacy estate ended up
owned by personal Gmail accounts in the first place.

Two credential shapes are supported, and the JSON payload distinguishes them
itself via its ``type`` field:

``external_account``
    Workload Identity Federation.  The JSON carries no key material -- it
    describes how to exchange an AWS instance identity (the Concourse worker's
    IAM role) for a short-lived Google access token.  This is the target state
    and the only shape that should exist for automation once the migration is
    done.

``service_account``
    A downloaded service-account key.  Long-lived key material, exactly the
    thing this project exists to get rid of.  Accepted because bootstrapping
    Workload Identity Federation requires an identity that predates it, but
    every use logs a warning naming the stack that still depends on one.
"""

import json
from pathlib import Path
from typing import Any

import pulumi
import pulumi_gcp as gcp

from bridge.secrets.sops import read_yaml_secrets

# Least-privilege default. Widen per stack only with a comment saying why.
DEFAULT_SCOPES = ["https://www.googleapis.com/auth/cloud-platform"]

WORKLOAD_IDENTITY = "external_account"
SERVICE_ACCOUNT_KEY = "service_account"  # pragma: allowlist secret


def read_gcp_credentials(
    sops_path: Path = Path("gcp/credentials.yaml"),
    key: str = "credentials",
) -> str:
    """Read a GCP credential document out of the SOPS secret store.

    The secret holds the credential JSON as a string under ``key`` so that
    either credential shape round-trips unmodified -- Google's client
    libraries parse the document themselves and are particular about it.

    :param sops_path: Path of the SOPS file, relative to ``src/bridge/secrets``.
    :param key: Top-level key in that file holding the credential JSON.

    :returns: The credential JSON, verbatim.
    """
    secrets = read_yaml_secrets(sops_path)
    credentials = secrets[key]
    if isinstance(credentials, dict):
        # Tolerate the credential being stored as nested YAML rather than as an
        # embedded JSON string, since both are natural things to write.
        return json.dumps(credentials)
    return credentials


def credential_type(credentials: str) -> str:
    """Return the ``type`` field of a GCP credential document."""
    return json.loads(credentials).get("type", "")


def gcp_provider(
    name: str,
    project: str,
    credentials: str | None = None,
    region: str | None = None,
    scopes: list[str] | None = None,
    **provider_args: Any,
) -> gcp.Provider:
    """Construct a GCP provider pinned to an explicit project and identity.

    :param name: Pulumi resource name for the provider.
    :param project: GCP project id the provider operates in. Always explicit --
        the provider's own fallback is the ``CLOUDSDK_CORE_PROJECT``/gcloud
        config value, which is whatever the operator last ran ``gcloud config
        set project`` with.
    :param credentials: Credential JSON. Defaults to the SOPS-stored document.
    :param region: Default region for regional resources.
    :param scopes: OAuth scopes. Defaults to ``cloud-platform``.

    :returns: A configured provider, to be passed to every GCP resource in the
        stack via ``ResourceOptions(provider=...)``.
    """
    credential_document = credentials or read_gcp_credentials()
    document_type = credential_type(credential_document)
    if document_type == SERVICE_ACCOUNT_KEY:
        pulumi.log.warn(
            f"GCP provider {name} is authenticating with a downloaded service "
            "account key. Replace it with Workload Identity Federation "
            "(type=external_account) once the target project supports it."
        )
    elif document_type != WORKLOAD_IDENTITY:
        msg = (
            f"Unsupported GCP credential type {document_type!r}. Expected "
            f"{WORKLOAD_IDENTITY!r} or {SERVICE_ACCOUNT_KEY!r}."
        )
        raise ValueError(msg)
    return gcp.Provider(
        name,
        project=project,
        credentials=pulumi.Output.secret(credential_document),
        region=region,
        scopes=scopes or DEFAULT_SCOPES,
        **provider_args,
    )
