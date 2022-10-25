from collections import namedtuple
from enum import Enum
from typing import Literal, Optional, Union

from pydantic import BaseModel


class OpenEdxApplication(Enum):
    codejail = "codejail"
    edxapp = "edx-platform"
    forum = "forum"
    notes = "notes-api"
    xqueue = "xqueue"
    xqueue_watcher = "xqueue-watcher"
    theme = "edxapp_theme"


class OpenEdxMicroFrontend(Enum):
    course_authoring = "course-authoring"
    gradebook = "gradebook"
    learn = "learn"
    library_authoring = "library-authoring"


EnvRelease = namedtuple("EnvRelease", ("environment", "edx_release"))
EnvName = Literal["CI", "QA", "Production"]
DeploymentName = Literal["mitx", "mitx-staging", "mitxonline", "xpro"]
OpenEdxSupportedRelease = Literal["master", "olive", "maple", "nutmeg"]
OpenEdxBranchMap: dict[OpenEdxSupportedRelease, str] = {
    "master": "master",
    "olive": "open-release/olive.master",
    "nutmeg": "open-release/nutmeg.master",
    "maple": "open-release/maple.master",
}

OpenEdxRepoMap: dict[Union[OpenEdxApplication, OpenEdxMicroFrontend], str] = {
    OpenEdxApplication.edxapp: "https://github.com/openedx/edx-platform",
    OpenEdxApplication.forum: "https://github.com/openedx/cs_comments_service",
    OpenEdxApplication.notes: "https://github.com/openedx/edx-notes-api",
    OpenEdxApplication.xqueue: "https://github.com/openedx/xqueue",
    OpenEdxMicroFrontend.course_authoring: "https://github.com/openedx/frontend-app-course-authoring",
    OpenEdxMicroFrontend.gradebook: "https://github.com/openedx/frontend-app-gradebook",
    OpenEdxMicroFrontend.learn: "https://github.com/openedx/frontend-app-learning",
    OpenEdxMicroFrontend.library_authoring: "https://github.com/openedx/frontend-app-library-authoring",
}


class DeploymentEnvRelease(BaseModel):
    deployment_name: DeploymentName
    env_release_map: list[EnvRelease]

    def envs_by_release(
        self, release_name: OpenEdxSupportedRelease
    ) -> list[DeploymentName]:
        return [
            env_release.environment
            for env_release in self.env_release_map
            if env_release.edx_release == release_name
        ]

    def release_by_env(self, env_name: EnvName) -> Optional[OpenEdxSupportedRelease]:
        for env_release in self.env_release_map:
            if env_release.environment == env_name:
                return env_release.edx_release
        return None

    @property
    def environments(self) -> list[EnvName]:
        return [env_release.environment for env_release in self.env_release_map]


class OpenLearningOpenEdxDeployment(Enum):
    mitx = DeploymentEnvRelease(
        deployment_name="mitx",
        env_release_map=[
            EnvRelease("CI", "olive"),
            EnvRelease("QA", "nutmeg"),
            EnvRelease("Production", "nutmeg"),
        ],
    )
    mitx_staging = DeploymentEnvRelease(
        deployment_name="mitx-staging",
        env_release_map=[
            EnvRelease("CI", "olive"),
            EnvRelease("QA", "nutmeg"),
            EnvRelease("Production", "nutmeg"),
        ],
    )
    mitxonline = DeploymentEnvRelease(
        deployment_name="mitxonline",
        env_release_map=[
            EnvRelease("QA", "master"),
            EnvRelease("Production", "master"),
        ],
    )
    xpro = DeploymentEnvRelease(
        deployment_name="xpro",
        env_release_map=[
            EnvRelease("CI", "olive"),
            EnvRelease("QA", "maple"),
            EnvRelease("Production", "maple"),
        ],
    )


class OpenEdxApplicationVersion(BaseModel):
    application_name: Union[OpenEdxApplication, OpenEdxMicroFrontend]
    release_name: OpenEdxSupportedRelease
    # IDA == Independently Deployable Application
    # MFE == Micro Front-End
    application_type: Literal["MFE", "IDA"]
    branch_override: Optional[str]
    origin_override: Optional[str]

    @property
    def release_branch(self) -> str:
        if self.branch_override:
            return self.branch_override
        if self.application_name == "edx-platform" and self.release_name == "master":
            return "release"
        return OpenEdxBranchMap[self.release_name]

    @property
    def git_origin(self) -> str:
        return self.origin_override or OpenEdxRepoMap[self.application_name]


ReleaseMap: dict[  # noqa: WPS234
    OpenEdxSupportedRelease,
    dict[OpenLearningOpenEdxDeployment, list[OpenEdxApplicationVersion]],
] = {
    "olive": {
        OpenLearningOpenEdxDeployment.mitx: [
            OpenEdxApplicationVersion(
                application_name="edx-platform",  # type: ignore
                application_type="IDA",
                release_name="olive",
            ),
            OpenEdxApplicationVersion(
                application_name="edxapp_theme",  # type: ignore
                application_type="IDA",
                release_name="olive",
                branch_override="olive",
                origin_override="https://github.com/mitodl/mitx-theme",
            ),
            OpenEdxApplicationVersion(
                application_name="forum",  # type: ignore
                application_type="IDA",
                release_name="olive",
            ),
            OpenEdxApplicationVersion(
                application_name="xqueue",  # type: ignore
                application_type="IDA",
                release_name="olive",
            ),
            OpenEdxApplicationVersion(
                application_name="learn",  # type: ignore
                application_type="MFE",
                release_name="olive",
            ),
            OpenEdxApplicationVersion(
                application_name="gradebook",  # type: ignore
                application_type="MFE",
                release_name="olive",
            ),
            OpenEdxApplicationVersion(
                application_name="course-authoring",  # type: ignore
                application_type="MFE",
                release_name="olive",
            ),
        ],
        OpenLearningOpenEdxDeployment.mitx_staging: [
            OpenEdxApplicationVersion(
                application_name="edx-platform",  # type: ignore
                application_type="IDA",
                release_name="olive",
            ),
            OpenEdxApplicationVersion(
                application_name="edxapp_theme",  # type: ignore
                application_type="IDA",
                release_name="olive",
                branch_override="olive",
                origin_override="https://github.com/mitodl/mitx-theme",
            ),
            OpenEdxApplicationVersion(
                application_name="forum",  # type: ignore
                application_type="IDA",
                release_name="olive",
            ),
            OpenEdxApplicationVersion(
                application_name="xqueue",  # type: ignore
                application_type="IDA",
                release_name="olive",
            ),
            OpenEdxApplicationVersion(
                application_name="learn",  # type: ignore
                application_type="MFE",
                release_name="olive",
            ),
            OpenEdxApplicationVersion(
                application_name="gradebook",  # type: ignore
                application_type="MFE",
                release_name="olive",
            ),
            OpenEdxApplicationVersion(
                application_name="course-authoring",  # type: ignore
                application_type="MFE",
                release_name="olive",
            ),
        ],
        OpenLearningOpenEdxDeployment.xpro: [
            OpenEdxApplicationVersion(
                application_name="edx-platform",  # type: ignore
                application_type="IDA",
                release_name="olive",
            ),
            OpenEdxApplicationVersion(
                application_name="edxapp_theme",  # type: ignore
                application_type="IDA",
                release_name="olive",
                branch_override="olive",
                origin_override="https://github.com/mitodl/mitxpro-theme",
            ),
            OpenEdxApplicationVersion(
                application_name="forum",  # type: ignore
                application_type="IDA",
                release_name="olive",
            ),
            OpenEdxApplicationVersion(
                application_name="learn",  # type: ignore
                application_type="MFE",
                release_name="olive",
            ),
            OpenEdxApplicationVersion(
                application_name="gradebook",  # type: ignore
                application_type="MFE",
                release_name="olive",
            ),
        ],
    },
    "nutmeg": {
        OpenLearningOpenEdxDeployment.mitx: [
            OpenEdxApplicationVersion(
                application_name="edx-platform",  # type: ignore
                application_type="IDA",
                release_name="nutmeg",
                branch_override="mitx/nutmeg",
                origin_override="https://github.com/mitodl/edx-platform",
            ),
            OpenEdxApplicationVersion(
                application_name="edxapp_theme",  # type: ignore
                application_type="IDA",
                release_name="nutmeg",
                branch_override="nutmeg",
                origin_override="https://github.com/mitodl/mitx-theme",
            ),
            OpenEdxApplicationVersion(
                application_name="forum",  # type: ignore
                application_type="IDA",
                release_name="nutmeg",
            ),
            OpenEdxApplicationVersion(
                application_name="xqueue",  # type: ignore
                application_type="IDA",
                release_name="nutmeg",
            ),
            OpenEdxApplicationVersion(
                application_name="learn",  # type: ignore
                application_type="MFE",
                release_name="nutmeg",
            ),
            OpenEdxApplicationVersion(
                application_name="gradebook",  # type: ignore
                application_type="MFE",
                release_name="nutmeg",
            ),
            OpenEdxApplicationVersion(
                application_name="course-authoring",  # type: ignore
                application_type="MFE",
                release_name="nutmeg",
            ),
        ],
        OpenLearningOpenEdxDeployment.mitx_staging: [
            OpenEdxApplicationVersion(
                application_name="edx-platform",  # type: ignore
                application_type="IDA",
                release_name="nutmeg",
                branch_override="mitx/nutmeg",
                origin_override="https://github.com/mitodl/edx-platform",
            ),
            OpenEdxApplicationVersion(
                application_name="edxapp_theme",  # type: ignore
                application_type="IDA",
                release_name="nutmeg",
                branch_override="nutmeg",
                origin_override="https://github.com/mitodl/mitx-theme",
            ),
            OpenEdxApplicationVersion(
                application_name="forum",  # type: ignore
                application_type="IDA",
                release_name="nutmeg",
            ),
            OpenEdxApplicationVersion(
                application_name="xqueue",  # type: ignore
                application_type="IDA",
                release_name="nutmeg",
            ),
            OpenEdxApplicationVersion(
                application_name="learn",  # type: ignore
                application_type="MFE",
                release_name="nutmeg",
            ),
            OpenEdxApplicationVersion(
                application_name="gradebook",  # type: ignore
                application_type="MFE",
                release_name="nutmeg",
            ),
            OpenEdxApplicationVersion(
                application_name="course-authoring",  # type: ignore
                application_type="MFE",
                release_name="nutmeg",
            ),
        ],
    },
    "maple": {
        OpenLearningOpenEdxDeployment.xpro: [
            OpenEdxApplicationVersion(
                application_name="edx-platform",  # type: ignore
                application_type="IDA",
                release_name="maple",
            ),
            OpenEdxApplicationVersion(
                application_name="edxapp_theme",  # type: ignore
                application_type="IDA",
                release_name="maple",
                branch_override="maple",
                origin_override="https://github.com/mitodl/mitxpro-theme",
            ),
            OpenEdxApplicationVersion(
                application_name="forum",  # type: ignore
                application_type="IDA",
                release_name="maple",
            ),
            OpenEdxApplicationVersion(
                application_name="learn",  # type: ignore
                application_type="MFE",
                release_name="maple",
            ),
            OpenEdxApplicationVersion(
                application_name="gradebook",  # type: ignore
                application_type="MFE",
                release_name="maple",
            ),
        ],
    },
    "master": {
        OpenLearningOpenEdxDeployment.mitxonline: [
            OpenEdxApplicationVersion(
                application_name="edx-platform",  # type: ignore
                application_type="IDA",
                release_name="master",
                branch_override="release",
            ),
            OpenEdxApplicationVersion(
                application_name="edxapp_theme",  # type: ignore
                application_type="IDA",
                release_name="master",
                branch_override="main",
                origin_override="https://github.com/mitodl/mitxonline-theme",
            ),
            OpenEdxApplicationVersion(
                application_name="forum",  # type: ignore
                application_type="IDA",
                release_name="master",
            ),
            OpenEdxApplicationVersion(
                application_name="learn",  # type: ignore
                application_type="MFE",
                release_name="master",
                branch_override="open-learning",
                origin_override="https://github.com/mitodl/frontend-app-learning",
            ),
            OpenEdxApplicationVersion(
                application_name="gradebook",  # type: ignore
                application_type="MFE",
                release_name="master",
            ),
        ],
    },
}


def fetch_application_version(
    release_name: OpenEdxSupportedRelease,
    deployment: OpenLearningOpenEdxDeployment,
    application_name: Union[OpenEdxApplication, OpenEdxMicroFrontend],
) -> Optional[OpenEdxApplicationVersion]:
    app_versions = ReleaseMap[release_name][deployment]
    fetched_app_version = None
    for app_version in app_versions:
        if app_version.application_name == application_name:
            fetched_app_version = app_version
    return fetched_app_version


def filter_deployments_by_release(release: str) -> list[DeploymentEnvRelease]:
    filtered_deployments = []
    for deployment in OpenLearningOpenEdxDeployment:
        release_match = False
        for env_tuple in deployment.value.env_release_map:
            if release == env_tuple.edx_release:
                release_match = True
        if release_match:
            filtered_deployments.append(deployment.value)
    return filtered_deployments
