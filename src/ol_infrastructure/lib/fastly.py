from pathlib import Path
from typing import Union

import pulumi
import pulumi_fastly as fastly

from bridge.secrets.sops import read_yaml_secrets


def get_fastly_provider(
    wrap_in_pulumi_options: bool = True,
) -> Union[fastly.Provider, pulumi.ResourceOptions]:
    pulumi.Config("fastly")
    fastly_provider = fastly.Provider(
        "fastly-provider",
        api_key=read_yaml_secrets(Path("fastly.yaml"))["admin_api_key"],
    )
    if wrap_in_pulumi_options:
        fastly_provider = pulumi.ResourceOptions(provider=fastly_provider)
    return fastly_provider
