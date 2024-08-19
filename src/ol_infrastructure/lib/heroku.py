from functools import lru_cache, partial
from pathlib import Path

import pulumi
import pulumiverse_heroku

from bridge.secrets.sops import read_yaml_secrets


@lru_cache
def get_heroku_provider(heroku_user: str) -> pulumi.ResourceTransformationResult:
    heroku_api_key = read_yaml_secrets(
        Path().joinpath("heroku", f"secrets.{heroku_user}.yaml")
    )["apiKey"]
    return pulumiverse_heroku.Provider(
        resource_name=f"ol-heroku-provider-{heroku_user}",
        api_key=heroku_api_key,
    )


def set_heroku_provider(
    heroku_user: str, resource_args: pulumi.ResourceTransformationArgs
) -> pulumi.ResourceTransformationResult:
    if resource_args.type_.split(":")[0] == "heroku":
        resource_args.opts.provider = get_heroku_provider(heroku_user)
    return pulumi.ResourceTransformationResult(
        props=resource_args.props,
        opts=resource_args.opts,
    )


def setup_heroku_provider():
    heroku_user = pulumi.Config("heroku").get("user") or "odl-devops"
    pulumi.runtime.register_stack_transformation(
        partial(set_heroku_provider, heroku_user)
    )
