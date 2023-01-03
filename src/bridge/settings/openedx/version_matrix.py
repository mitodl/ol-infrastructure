from enum import Enum

from bridge.settings.openedx.types import (
    DeploymentEnvRelease,
    EnvRelease,
    OpenEdxApplicationVersion,
    OpenEdxDeploymentName,
    OpenEdxSupportedRelease,
)


class OpenLearningOpenEdxDeployment(Enum):
    mitx = DeploymentEnvRelease(
        deployment_name="mitx",
        env_release_map=[
            EnvRelease("CI", OpenEdxSupportedRelease["olive"]),
            EnvRelease("QA", OpenEdxSupportedRelease["olive"]),
            EnvRelease("Production", OpenEdxSupportedRelease["olive"]),
        ],
    )
    mitx_staging = DeploymentEnvRelease(
        deployment_name="mitx-staging",
        env_release_map=[
            EnvRelease("CI", OpenEdxSupportedRelease["olive"]),
            EnvRelease("QA", OpenEdxSupportedRelease["olive"]),
            EnvRelease("Production", OpenEdxSupportedRelease["olive"]),
        ],
    )
    mitxonline = DeploymentEnvRelease(
        deployment_name="mitxonline",
        env_release_map=[
            EnvRelease("QA", OpenEdxSupportedRelease["master"]),
            EnvRelease("Production", OpenEdxSupportedRelease["master"]),
        ],
    )
    xpro = DeploymentEnvRelease(
        deployment_name="xpro",
        env_release_map=[
            EnvRelease("CI", OpenEdxSupportedRelease["olive"]),
            EnvRelease("QA", OpenEdxSupportedRelease["olive"]),
            EnvRelease("Production", OpenEdxSupportedRelease["olive"]),
        ],
    )

    @classmethod
    def get_item(cls, key: OpenEdxDeploymentName) -> DeploymentEnvRelease:
        deployment_name_map = {
            "mitx": cls.mitx,
            "mitx-staging": cls.mitx_staging,
            "mitxonline": cls.mitxonline,
            "xpro": cls.xpro,
        }
        try:
            deployment = cls[key]
        except KeyError:
            deployment = deployment_name_map[key]
        return deployment.value


ReleaseMap: dict[  # noqa: WPS234
    OpenEdxSupportedRelease,
    dict[OpenEdxDeploymentName, list[OpenEdxApplicationVersion]],
] = {
    "olive": {
        "mitx": [
            OpenEdxApplicationVersion(
                application="edx-platform",  # type: ignore
                application_type="IDA",
                release="olive",
                branch_override="mitx/olive",
                origin_override="https://github.com/mitodl/edx-platform",
            ),
            OpenEdxApplicationVersion(
                application="edxapp_theme",  # type: ignore
                application_type="IDA",
                release="olive",
                branch_override="olive",
                origin_override="https://github.com/mitodl/mitx-theme",
            ),
            OpenEdxApplicationVersion(
                application="forum",  # type: ignore
                application_type="IDA",
                release="olive",
            ),
            OpenEdxApplicationVersion(
                application="xqueue",  # type: ignore
                application_type="IDA",
                release="olive",
            ),
            OpenEdxApplicationVersion(
                application="notes-api",  # type: ignore
                application_type="IDA",
                release="olive",
            ),
            OpenEdxApplicationVersion(
                application="learning",  # type: ignore
                application_type="MFE",
                release="olive",
            ),
            OpenEdxApplicationVersion(
                application="gradebook",  # type: ignore
                application_type="MFE",
                release="olive",
            ),
            OpenEdxApplicationVersion(
                application="course-authoring",  # type: ignore
                application_type="MFE",
                release="olive",
            ),
            OpenEdxApplicationVersion(
                application="library-authoring",  # type: ignore
                application_type="MFE",
                release="olive",
                branch_override="master",
            ),
            OpenEdxApplicationVersion(
                application="ora-grading",  # type: ignore
                application_type="MFE",
                release="olive",
            ),
        ],
        "mitx-staging": [
            OpenEdxApplicationVersion(
                application="edx-platform",  # type: ignore
                application_type="IDA",
                release="olive",
                branch_override="mitx/olive",
                origin_override="https://github.com/mitodl/edx-platform",
            ),
            OpenEdxApplicationVersion(
                application="edxapp_theme",  # type: ignore
                application_type="IDA",
                release="olive",
                branch_override="olive",
                origin_override="https://github.com/mitodl/mitx-theme",
            ),
            OpenEdxApplicationVersion(
                application="forum",  # type: ignore
                application_type="IDA",
                release="olive",
            ),
            OpenEdxApplicationVersion(
                application="xqueue",  # type: ignore
                application_type="IDA",
                release="olive",
            ),
            OpenEdxApplicationVersion(
                application="notes-api",  # type: ignore
                application_type="IDA",
                release="olive",
            ),
            OpenEdxApplicationVersion(
                application="learning",  # type: ignore
                application_type="MFE",
                release="olive",
            ),
            OpenEdxApplicationVersion(
                application="gradebook",  # type: ignore
                application_type="MFE",
                release="olive",
            ),
            OpenEdxApplicationVersion(
                application="course-authoring",  # type: ignore
                application_type="MFE",
                release="olive",
            ),
            OpenEdxApplicationVersion(
                application="library-authoring",  # type: ignore
                application_type="MFE",
                release="olive",
                branch_override="master",
            ),
            OpenEdxApplicationVersion(
                application="ora-grading",  # type: ignore
                application_type="MFE",
                release="olive",
            ),
        ],
        "xpro": [
            OpenEdxApplicationVersion(
                application="edx-platform",  # type: ignore
                application_type="IDA",
                release="olive",
            ),
            OpenEdxApplicationVersion(
                application="edxapp_theme",  # type: ignore
                application_type="IDA",
                release="olive",
                branch_override="olive",
                origin_override="https://github.com/mitodl/mitxpro-theme",
            ),
            OpenEdxApplicationVersion(
                application="forum",  # type: ignore
                application_type="IDA",
                release="olive",
            ),
            OpenEdxApplicationVersion(
                application="notes-api",  # type: ignore
                application_type="IDA",
                release="olive",
            ),
            OpenEdxApplicationVersion(
                application="learning",  # type: ignore
                application_type="MFE",
                release="olive",
            ),
            OpenEdxApplicationVersion(
                application="gradebook",  # type: ignore
                application_type="MFE",
                release="olive",
            ),
            OpenEdxApplicationVersion(
                application="library-authoring",  # type: ignore
                application_type="MFE",
                release="olive",
                branch_override="master",
            ),
            OpenEdxApplicationVersion(
                application="course-authoring",  # type: ignore
                application_type="MFE",
                release="olive",
                branch_override="master",
            ),
            OpenEdxApplicationVersion(
                application="ora-grading",  # type: ignore
                application_type="MFE",
                release="olive",
            ),
        ],
    },
    "master": {
        "mitxonline": [
            OpenEdxApplicationVersion(
                application="edx-platform",  # type: ignore
                application_type="IDA",
                release="master",
                branch_override="release",
            ),
            OpenEdxApplicationVersion(
                application="edxapp_theme",  # type: ignore
                application_type="IDA",
                release="master",
                branch_override="main",
                origin_override="https://github.com/mitodl/mitxonline-theme",
            ),
            OpenEdxApplicationVersion(
                application="forum",  # type: ignore
                application_type="IDA",
                release="master",
            ),
            OpenEdxApplicationVersion(
                application="learning",  # type: ignore
                application_type="MFE",
                release="master",
                branch_override="open-learning",
                origin_override="https://github.com/mitodl/frontend-app-learning",
            ),
            OpenEdxApplicationVersion(
                application="gradebook",  # type: ignore
                application_type="MFE",
                release="master",
            ),
            OpenEdxApplicationVersion(
                application="library-authoring",  # type: ignore
                application_type="MFE",
                release="master",
            ),
            OpenEdxApplicationVersion(
                application="course-authoring",  # type: ignore
                application_type="MFE",
                release="master",
            ),
        ],
    },
}
