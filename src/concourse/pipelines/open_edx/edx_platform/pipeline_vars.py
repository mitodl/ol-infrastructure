from collections import namedtuple
from typing import Literal

from pydantic import BaseModel

DeploymentName = Literal["mitx", "mitx-staging", "mitxonline", "xpro"]
EdxSupportedRelease = Literal["maple", "nutmeg", "olive", "master"]
EnvName = Literal["CI", "QA", "Production"]
EnvRelease = namedtuple("EnvRelease", ("environment", "edx_release"))
RELEASE_MAP = {  # noqa: WPS407
    "master": "release",
    "olive": "open-release/olive.master",
    "nutmeg": "open-release/nutmeg.master",
    "maple": "open-release/maple.master",
}


class DeploymentEnvRelease(BaseModel):
    deployment_name: DeploymentName
    env_release_map: list[EnvRelease]


ol_edx_deployments = [
    DeploymentEnvRelease(
        deployment_name="mitx",
        env_release_map=[
            EnvRelease("CI", "olive"),
            EnvRelease("QA", "nutmeg"),
            EnvRelease("Production", "nutmeg"),
        ],
    ),
    DeploymentEnvRelease(
        deployment_name="mitx-staging",
        env_release_map=[
            EnvRelease("CI", "olive"),
            EnvRelease("QA", "nutmeg"),
            EnvRelease("Production", "nutmeg"),
        ],
    ),
    DeploymentEnvRelease(
        deployment_name="xpro",
        env_release_map=[
            EnvRelease("CI", "olive"),
            EnvRelease("QA", "maple"),
            EnvRelease("Production", "maple"),
        ],
    ),
    DeploymentEnvRelease(
        deployment_name="mitxonline",
        env_release_map=[
            EnvRelease("QA", "master"),
            EnvRelease("Production", "master"),
        ],
    ),
]
