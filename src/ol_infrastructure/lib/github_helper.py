"""Stack transformation that wires github:* resources to the shared GitHub App provider.

Same pattern as vault.setup_vault_provider / eks_helper.setup_k8s_provider.

Also exposes `get_installation_token()` for the out-of-band tooling that reads the org
through the same App credentials the provider uses -- `bin/github-org-inventory` and
`scripts/github/verify_app_permissions.py`. Keeping that here means there is one place
that knows how the App authenticates.
"""

import time
from functools import lru_cache, partial
from pathlib import Path
from typing import Any

import httpx
import jwt
import pulumi
import pulumi_github as github

from bridge.secrets.sops import read_yaml_secrets

GITHUB_API = "https://api.github.com"
API_HEADERS = {
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}
# GitHub caps App JWTs at 10 minutes; stay comfortably inside it.
_JWT_LIFETIME_SECONDS = 540
_CLOCK_SKEW_SECONDS = 60


@lru_cache
def get_github_provider(
    owner: str,
    provider_name: str | None = None,
) -> github.Provider:
    github_app_secrets = read_yaml_secrets(Path("pulumi/github_app.yaml"))
    return github.Provider(
        provider_name or "github-provider",
        owner=owner,
        app_auth=github.ProviderAppAuthArgs(
            id=str(github_app_secrets["app_id"]),
            installation_id=str(github_app_secrets["installation_id"]),
            pem_file=github_app_secrets["private_key"],
        ),
    )


def get_installation_token() -> str:
    """Mint a short-lived installation access token for the App.

    The token carries exactly the permissions granted to the installation, which makes
    it the right credential for org-wide reads: a user PAT would need admin on each repo
    individually. Tokens expire after an hour -- mint per run, never cache to disk.
    """
    secrets: dict[str, Any] = read_yaml_secrets(Path("pulumi/github_app.yaml"))
    now = int(time.time())
    assertion = jwt.encode(
        {
            "iat": now - _CLOCK_SKEW_SECONDS,
            "exp": now + _JWT_LIFETIME_SECONDS,
            "iss": str(secrets["app_id"]),
        },
        secrets["private_key"],
        algorithm="RS256",
    )
    response = httpx.post(
        f"{GITHUB_API}/app/installations/{secrets['installation_id']}/access_tokens",
        headers={**API_HEADERS, "Authorization": f"Bearer {assertion}"},
        timeout=30,
    )
    response.raise_for_status()
    return str(response.json()["token"])


def set_github_provider(
    owner: str,
    provider_name: str | None,
    resource_args: pulumi.ResourceTransformationArgs,
) -> pulumi.ResourceTransformationResult:
    if resource_args.type_.split(":")[0] == "github":
        resource_args.opts.provider = get_github_provider(owner, provider_name)
    return pulumi.ResourceTransformationResult(
        props=resource_args.props,
        opts=resource_args.opts,
    )


def setup_github_provider(
    owner: str = "mitodl",
    provider_name: str | None = None,
) -> github.Provider:
    pulumi.runtime.register_stack_transformation(
        partial(set_github_provider, owner, provider_name)
    )
    return get_github_provider(owner, provider_name)
