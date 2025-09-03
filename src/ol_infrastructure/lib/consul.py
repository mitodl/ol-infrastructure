from pathlib import Path

import pulumi
import pulumi_consul as consul

from bridge.secrets.sops import read_yaml_secrets
from ol_infrastructure.lib.pulumi_helper import StackInfo


def get_consul_provider(
    stack_info: StackInfo,
    wrap_in_pulumi_options: bool = True,  # noqa: FBT001, FBT002
    consul_address: str | None = None,
    provider_name: str = "consul-provider",
) -> consul.Provider | pulumi.ResourceOptions:
    consul_config = pulumi.Config("consul")
    consul_provider = consul.Provider(
        provider_name,
        address=consul_address or consul_config.get("address"),
        scheme="https",
        http_auth="pulumi:{}".format(
            read_yaml_secrets(Path(f"pulumi/consul.{stack_info.env_suffix}.yaml"))[
                "basic_auth_password"
            ]
        ),
    )
    if wrap_in_pulumi_options:
        consul_provider = pulumi.ResourceOptions(provider=consul_provider)
    return consul_provider


def consul_key_helper(key_value_mapping: dict[str, str]):
    keys = []
    for key, val in key_value_mapping.items():
        keys.append(
            consul.KeysKeyArgs(
                path=key,
                value=val,
            )
        )
    return keys
