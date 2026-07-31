"""Stack transformation that wires github:* resources to the shared GitHub App provider.

Same pattern as vault.setup_vault_provider / eks_helper.setup_k8s_provider.
"""

from functools import lru_cache, partial
from pathlib import Path

import pulumi
import pulumi_github as github

from bridge.secrets.sops import read_yaml_secrets


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
