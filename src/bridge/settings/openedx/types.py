from enum import Enum, EnumMeta
from typing import Literal, NamedTuple

from pydantic import BaseModel


class EnumInvertedLookupMeta(EnumMeta):
    def __getitem__(cls, item: str):
        inverse_map = {v.value: v for k, v in cls._member_map_.items()}
        try:
            return cls._member_map_[item]
        except KeyError:
            return inverse_map[item]


class OpenEdxApplication(str, Enum):
    repository: str

    def __new__(cls, app: str, repository: str):
        enum_element = str.__new__(cls, app)
        enum_element._value_ = app
        enum_element.repository = repository
        return enum_element

    codejail = ("codejail", "https://github.com/eduNEXT/codejailservice/")
    edxapp = ("edx-platform", "https://github.com/openedx/edx-platform")
    forum = ("forum", "https://github.com/openedx/cs_comments_service")
    notes = ("notes-api", "https://github.com/openedx/edx-notes-api")
    theme = ("edxapp_theme", "")
    xqueue = ("xqueue", "https://github.com/openedx/xqueue")
    xqwatcher = ("xqwatcher", "https://github.com/openedx/xqueue-watcher")


class OpenEdxMicroFrontend(str, Enum, metaclass=EnumInvertedLookupMeta):
    repository: str
    path: str

    def __new__(cls, app: str, repository: str, path: str):
        enum_element = str.__new__(cls, app)
        enum_element._value_ = app
        enum_element.repository = repository
        enum_element.path = path
        return enum_element

    communications = (
        "communications",
        "https://github.com/openedx/frontend-app-communications",
        "communications",
    )
    course_authoring = (
        "authoring",
        "https://github.com/openedx/frontend-app-authoring",
        "authoring",
    )
    discussion = (
        "discussions",
        "https://github.com/openedx/frontend-app-discussions",
        "discuss",
    )
    gradebook = (
        "gradebook",
        "https://github.com/openedx/frontend-app-gradebook",
        "gradebook",
    )
    learn = ("learning", "https://github.com/openedx/frontend-app-learning", "learn")
    learner_dashboard = (
        "learner-dashboard",
        "https://github.com/openedx/frontend-app-learner-dashboard",
        "dashboard",
    )
    ora_grading = (
        "ora-grading",
        "https://github.com/openedx/frontend-app-ora-grading",
        "ora-grading",
    )


EnvStage = Literal["CI", "QA", "Production"]
# IDA == Independently Deployable Application
# MFE == Micro Front-End
OpenEdxApplicationType = Literal["MFE", "IDA"]
OpenEdxDeploymentName = Literal["mitx", "mitx-staging", "mitxonline", "xpro"]


class OpenEdxSupportedRelease(str, Enum):
    branch: str
    python_version: str
    node_version: str

    def __new__(
        cls,
        release_name: str,
        release_branch: str,
        python_version: str,
        node_version: str,
    ):
        enum_element = str.__new__(cls, release_name)
        enum_element._value_ = release_name
        enum_element.branch = release_branch
        enum_element.python_version = python_version
        enum_element.node_version = node_version
        return enum_element

    master = ("master", "master", "3.11", "20")
    teak = ("teak", "release/teak", "3.11", "20")

    def __str__(self):
        return self.value


class EnvRelease(NamedTuple):
    environment: EnvStage
    edx_release: OpenEdxSupportedRelease


class DeploymentEnvRelease(BaseModel):
    deployment_name: OpenEdxDeploymentName
    env_release_map: list[EnvRelease]

    def envs_by_release(self, release_name: OpenEdxSupportedRelease) -> list[EnvStage]:
        return [
            env_release.environment
            for env_release in self.env_release_map
            if env_release.edx_release == release_name
        ]

    def release_by_env(self, env_stage: EnvStage) -> OpenEdxSupportedRelease | None:
        for env_release in self.env_release_map:
            if env_release.environment == env_stage:
                return env_release.edx_release
        return None

    @property
    def environments(self) -> list[EnvStage]:
        return [env_release.environment for env_release in self.env_release_map]

    @property
    def releases(self) -> set[OpenEdxSupportedRelease]:
        return {env_release.edx_release for env_release in self.env_release_map}


class OpenEdxApplicationVersion(BaseModel):
    application: OpenEdxApplication | OpenEdxMicroFrontend
    release: OpenEdxSupportedRelease
    application_type: OpenEdxApplicationType
    branch_override: str | None = None
    origin_override: str | None = None
    runtime_version_override: str | None = None
    translation_overrides: list[str] | None = None

    @property
    def runtime_version(self) -> str:
        # Default to Python 3.8 for IDAs and Node 16 for MFEs
        app_type_runtime = (
            self.release.python_version
            if self.application_type == "IDA"
            else self.release.node_version
        )
        return self.runtime_version_override or app_type_runtime

    @property
    def release_branch(self) -> str:
        if self.branch_override:
            return self.branch_override
        return self.release.branch

    @property
    def git_origin(self) -> str:
        return self.origin_override or self.application.repository
