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
            EnvRelease("CI", OpenEdxSupportedRelease["quince"]),
            EnvRelease("QA", OpenEdxSupportedRelease["quince"]),
            EnvRelease("Production", OpenEdxSupportedRelease["palm"]),
        ],
    )
    mitx_staging = DeploymentEnvRelease(
        deployment_name="mitx-staging",
        env_release_map=[
            EnvRelease("CI", OpenEdxSupportedRelease["quince"]),
            EnvRelease("QA", OpenEdxSupportedRelease["quince"]),
            EnvRelease("Production", OpenEdxSupportedRelease["palm"]),
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
            EnvRelease("CI", OpenEdxSupportedRelease["quince"]),
            EnvRelease("QA", OpenEdxSupportedRelease["quince"]),
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


default_branding_overrides = {
    "@edx/frontend-component-footer@npm": (
        "@mitodl/frontend-component-footer-mitol@latest"
    ),
    "@edx/frontend-component-header@npm": (
        "@mitodl/frontend-component-header-mitol@latest"
    ),
}


ReleaseMap: dict[
    OpenEdxSupportedRelease,
    dict[OpenEdxDeploymentName, list[OpenEdxApplicationVersion]],
] = {
    "quince": {
        "mitx": [
            OpenEdxApplicationVersion(
                application="communications",
                application_type="MFE",
                release="quince",
            ),
            OpenEdxApplicationVersion(
                application="course-authoring",
                application_type="MFE",
                release="quince",
                branding_overrides=default_branding_overrides,
            ),
            OpenEdxApplicationVersion(
                application="edx-platform",
                application_type="IDA",
                release="quince",
                branch_override="mitx/quince",
                origin_override="https://github.com/mitodl/edx-platform",
            ),
            OpenEdxApplicationVersion(
                application="edxapp_theme",
                application_type="IDA",
                release="quince",
                branch_override="quince",
                origin_override="https://github.com/mitodl/mitx-theme",
            ),
            OpenEdxApplicationVersion(
                application="forum",
                application_type="IDA",
                release="quince",
            ),
            OpenEdxApplicationVersion(
                application="gradebook",
                application_type="MFE",
                release="quince",
            ),
            OpenEdxApplicationVersion(
                application="learner-dashboard",
                application_type="MFE",
                release="quince",
                branding_overrides={
                    "@edx/brand@npm": "@mitodl/brand-mitol-residential@latest",
                    **default_branding_overrides,
                },
            ),
            OpenEdxApplicationVersion(
                application="learning",
                application_type="MFE",
                release="quince",
                branding_overrides=default_branding_overrides,
            ),
            OpenEdxApplicationVersion(
                application="library-authoring",
                application_type="MFE",
                release="quince",
                branch_override="master",
                branding_overrides=default_branding_overrides,
            ),
            OpenEdxApplicationVersion(
                application="notes-api",
                application_type="IDA",
                release="quince",
            ),
            OpenEdxApplicationVersion(
                application="ora-grading",
                application_type="MFE",
                release="quince",
                branding_overrides=default_branding_overrides,
            ),
            OpenEdxApplicationVersion(
                application="xqueue",
                application_type="IDA",
                release="quince",
            ),
        ],
        "mitx-staging": [
            OpenEdxApplicationVersion(
                application="communications",
                application_type="MFE",
                release="quince",
            ),
            OpenEdxApplicationVersion(
                application="course-authoring",
                application_type="MFE",
                release="quince",
                branding_overrides=default_branding_overrides,
            ),
            OpenEdxApplicationVersion(
                application="edx-platform",
                application_type="IDA",
                release="quince",
                branch_override="mitx/quince",
                origin_override="https://github.com/mitodl/edx-platform",
            ),
            OpenEdxApplicationVersion(
                application="edxapp_theme",
                application_type="IDA",
                release="quince",
                branch_override="quince",
                origin_override="https://github.com/mitodl/mitx-theme",
            ),
            OpenEdxApplicationVersion(
                application="forum",
                application_type="IDA",
                release="quince",
            ),
            OpenEdxApplicationVersion(
                application="gradebook",
                application_type="MFE",
                release="quince",
            ),
            OpenEdxApplicationVersion(
                application="learning",
                application_type="MFE",
                release="quince",
                branding_overrides=default_branding_overrides,
            ),
            OpenEdxApplicationVersion(
                application="learner-dashboard",
                application_type="MFE",
                release="quince",
                branding_overrides={
                    "@edx/brand@npm": "@mitodl/brand-mitol-residential@latest",
                    **default_branding_overrides,
                },
            ),
            OpenEdxApplicationVersion(
                application="library-authoring",
                application_type="MFE",
                release="quince",
                branch_override="master",
                branding_overrides=default_branding_overrides,
            ),
            OpenEdxApplicationVersion(
                application="notes-api",
                application_type="IDA",
                release="quince",
            ),
            OpenEdxApplicationVersion(
                application="ora-grading",
                application_type="MFE",
                release="quince",
                branding_overrides=default_branding_overrides,
            ),
            OpenEdxApplicationVersion(
                application="xqueue",
                application_type="IDA",
                release="quince",
            ),
        ],
        "xpro": [
            OpenEdxApplicationVersion(
                application="course-authoring",
                application_type="MFE",
                release="quince",
                branding_overrides=default_branding_overrides,
            ),
            OpenEdxApplicationVersion(
                application="edx-platform",
                application_type="IDA",
                release="quince",
            ),
            OpenEdxApplicationVersion(
                application="edxapp_theme",
                application_type="IDA",
                release="quince",
                branch_override="quince",
                origin_override="https://github.com/mitodl/mitxpro-theme",
            ),
            OpenEdxApplicationVersion(
                application="forum",
                application_type="IDA",
                release="quince",
            ),
            OpenEdxApplicationVersion(
                application="gradebook",
                application_type="MFE",
                release="quince",
            ),
            OpenEdxApplicationVersion(
                application="learning",
                application_type="MFE",
                release="quince",
                branding_overrides=default_branding_overrides,
            ),
            OpenEdxApplicationVersion(
                application="library-authoring",
                application_type="MFE",
                release="quince",
                branch_override="master",
                branding_overrides=default_branding_overrides,
            ),
            OpenEdxApplicationVersion(
                application="notes-api",
                application_type="IDA",
                release="quince",
            ),
            OpenEdxApplicationVersion(
                application="ora-grading",
                application_type="MFE",
                release="quince",
                branding_overrides=default_branding_overrides,
            ),
        ],
    },
    "palm": {
        "mitx": [
            OpenEdxApplicationVersion(
                application="communications",
                application_type="MFE",
                release="palm",
            ),
            OpenEdxApplicationVersion(
                application="course-authoring",
                application_type="MFE",
                release="palm",
                branding_overrides=default_branding_overrides,
            ),
            OpenEdxApplicationVersion(
                application="edx-platform",
                application_type="IDA",
                release="palm",
                branch_override="mitx/palm",
                origin_override="https://github.com/mitodl/edx-platform",
            ),
            OpenEdxApplicationVersion(
                application="edxapp_theme",
                application_type="IDA",
                release="palm",
                branch_override="palm",
                origin_override="https://github.com/mitodl/mitx-theme",
            ),
            OpenEdxApplicationVersion(
                application="forum",
                application_type="IDA",
                release="palm",
            ),
            OpenEdxApplicationVersion(
                application="gradebook",
                application_type="MFE",
                release="palm",
            ),
            OpenEdxApplicationVersion(
                application="learner-dashboard",
                application_type="MFE",
                release="palm",
                branding_overrides={
                    "@edx/brand@npm": "@mitodl/brand-mitol-residential@latest",
                    **default_branding_overrides,
                },
            ),
            OpenEdxApplicationVersion(
                application="learning",
                application_type="MFE",
                release="palm",
                branding_overrides=default_branding_overrides,
            ),
            OpenEdxApplicationVersion(
                application="library-authoring",
                application_type="MFE",
                release="palm",
                branch_override="master",
                branding_overrides=default_branding_overrides,
            ),
            OpenEdxApplicationVersion(
                application="notes-api",
                application_type="IDA",
                release="palm",
            ),
            OpenEdxApplicationVersion(
                application="ora-grading",
                application_type="MFE",
                release="palm",
                branding_overrides=default_branding_overrides,
            ),
            OpenEdxApplicationVersion(
                application="xqueue",
                application_type="IDA",
                release="palm",
            ),
        ],
        "mitx-staging": [
            OpenEdxApplicationVersion(
                application="communications",
                application_type="MFE",
                release="palm",
            ),
            OpenEdxApplicationVersion(
                application="course-authoring",
                application_type="MFE",
                release="palm",
                branding_overrides=default_branding_overrides,
            ),
            OpenEdxApplicationVersion(
                application="edx-platform",
                application_type="IDA",
                release="palm",
                branch_override="mitx/palm",
                origin_override="https://github.com/mitodl/edx-platform",
            ),
            OpenEdxApplicationVersion(
                application="edxapp_theme",
                application_type="IDA",
                release="palm",
                branch_override="palm",
                origin_override="https://github.com/mitodl/mitx-theme",
            ),
            OpenEdxApplicationVersion(
                application="forum",
                application_type="IDA",
                release="palm",
            ),
            OpenEdxApplicationVersion(
                application="gradebook",
                application_type="MFE",
                release="palm",
            ),
            OpenEdxApplicationVersion(
                application="learning",
                application_type="MFE",
                release="palm",
                branding_overrides=default_branding_overrides,
            ),
            OpenEdxApplicationVersion(
                application="learner-dashboard",
                application_type="MFE",
                release="palm",
                branding_overrides={
                    "@edx/brand@npm": "@mitodl/brand-mitol-residential@latest",
                    **default_branding_overrides,
                },
            ),
            OpenEdxApplicationVersion(
                application="library-authoring",
                application_type="MFE",
                release="palm",
                branch_override="master",
                branding_overrides=default_branding_overrides,
            ),
            OpenEdxApplicationVersion(
                application="notes-api",
                application_type="IDA",
                release="palm",
            ),
            OpenEdxApplicationVersion(
                application="ora-grading",
                application_type="MFE",
                release="palm",
                branding_overrides=default_branding_overrides,
            ),
            OpenEdxApplicationVersion(
                application="xqueue",
                application_type="IDA",
                release="palm",
            ),
        ],
    },
    "olive": {
        "xpro": [
            OpenEdxApplicationVersion(
                application="course-authoring",
                application_type="MFE",
                release="olive",
                runtime_version_override="16",
                branding_overrides=default_branding_overrides,
            ),
            OpenEdxApplicationVersion(
                application="edx-platform",
                application_type="IDA",
                release="olive",
            ),
            OpenEdxApplicationVersion(
                application="edxapp_theme",
                application_type="IDA",
                release="olive",
                branch_override="olive",
                origin_override="https://github.com/mitodl/mitxpro-theme",
            ),
            OpenEdxApplicationVersion(
                application="forum",
                application_type="IDA",
                release="olive",
            ),
            OpenEdxApplicationVersion(
                application="gradebook",
                application_type="MFE",
                release="olive",
                runtime_version_override="16",
            ),
            OpenEdxApplicationVersion(
                application="learning",
                application_type="MFE",
                release="olive",
                runtime_version_override="16",
                branding_overrides=default_branding_overrides,
            ),
            OpenEdxApplicationVersion(
                application="library-authoring",
                application_type="MFE",
                release="olive",
                branch_override="master",
                runtime_version_override="16",
                branding_overrides=default_branding_overrides,
            ),
            OpenEdxApplicationVersion(
                application="notes-api",
                application_type="IDA",
                release="olive",
            ),
            OpenEdxApplicationVersion(
                application="ora-grading",
                application_type="MFE",
                release="olive",
                runtime_version_override="16",
                branding_overrides=default_branding_overrides,
            ),
        ],
    },
    "master": {
        "mitxonline": [
            OpenEdxApplicationVersion(
                application="communications",
                application_type="MFE",
                release="master",
            ),
            OpenEdxApplicationVersion(
                application="course-authoring",
                application_type="MFE",
                release="master",
                branding_overrides=default_branding_overrides,
            ),
            OpenEdxApplicationVersion(
                application="edx-platform",
                application_type="IDA",
                release="master",
                branch_override="2u/release",
            ),
            OpenEdxApplicationVersion(
                application="edxapp_theme",
                application_type="IDA",
                release="master",
                branch_override="main",
                origin_override="https://github.com/mitodl/mitxonline-theme",
            ),
            OpenEdxApplicationVersion(
                application="forum",
                application_type="IDA",
                release="master",
            ),
            OpenEdxApplicationVersion(
                application="gradebook",
                application_type="MFE",
                release="master",
            ),
            OpenEdxApplicationVersion(
                application="learning",
                application_type="MFE",
                release="master",
                branch_override="open-learning",
                origin_override="https://github.com/mitodl/frontend-app-learning",
                branding_overrides=default_branding_overrides,
            ),
            OpenEdxApplicationVersion(
                application="library-authoring",
                application_type="MFE",
                release="master",
                branding_overrides=default_branding_overrides,
            ),
            OpenEdxApplicationVersion(
                application="ora-grading",
                application_type="MFE",
                release="master",
                branding_overrides=default_branding_overrides,
            ),
        ],
    },
}
