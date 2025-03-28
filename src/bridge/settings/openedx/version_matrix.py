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
            EnvRelease("CI", OpenEdxSupportedRelease["sumac"]),
            EnvRelease("QA", OpenEdxSupportedRelease["sumac"]),
            EnvRelease("Production", OpenEdxSupportedRelease["sumac"]),
        ],
    )
    mitx_staging = DeploymentEnvRelease(
        deployment_name="mitx-staging",
        env_release_map=[
            EnvRelease("CI", OpenEdxSupportedRelease["sumac"]),
            EnvRelease("QA", OpenEdxSupportedRelease["sumac"]),
            EnvRelease("Production", OpenEdxSupportedRelease["sumac"]),
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
            EnvRelease("CI", OpenEdxSupportedRelease["sumac"]),
            EnvRelease("QA", OpenEdxSupportedRelease["sumac"]),
            EnvRelease("Production", OpenEdxSupportedRelease["sumac"]),
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
}


ReleaseMap: dict[
    OpenEdxSupportedRelease,
    dict[OpenEdxDeploymentName, list[OpenEdxApplicationVersion]],
] = {
    "sumac": {
        "mitx": [
            OpenEdxApplicationVersion(
                application="codejail",
                application_type="IDA",
                release="sumac",
            ),
            OpenEdxApplicationVersion(
                application="communications",
                application_type="MFE",
                release="sumac",
                branding_overrides=default_branding_overrides,
            ),
            OpenEdxApplicationVersion(
                application="authoring",
                application_type="MFE",
                release="sumac",
                branding_overrides=default_branding_overrides,
            ),
            OpenEdxApplicationVersion(
                application="discussions",
                application_type="MFE",
                release="sumac",
                branding_overrides=default_branding_overrides,
            ),
            OpenEdxApplicationVersion(
                application="edx-platform",
                application_type="IDA",
                release="sumac",
                branch_override="mitx/sumac",
                origin_override="https://github.com/mitodl/edx-platform",
                runtime_version_override="3.11",
            ),
            OpenEdxApplicationVersion(
                application="edxapp_theme",
                application_type="IDA",
                release="sumac",
                branch_override="sumac",
                origin_override="https://github.com/mitodl/mitx-theme",
            ),
            OpenEdxApplicationVersion(
                application="forum",
                application_type="IDA",
                release="sumac",
            ),
            OpenEdxApplicationVersion(
                application="gradebook",
                application_type="MFE",
                release="sumac",
                branding_overrides=default_branding_overrides,
            ),
            OpenEdxApplicationVersion(
                application="learner-dashboard",
                application_type="MFE",
                release="sumac",
                branding_overrides={
                    "@edx/brand@npm": "@mitodl/brand-mitol-residential@latest",
                    **default_branding_overrides,
                },
            ),
            OpenEdxApplicationVersion(
                application="learning",
                application_type="MFE",
                release="sumac",
                branding_overrides=default_branding_overrides,
            ),
            OpenEdxApplicationVersion(
                application="notes-api",
                application_type="IDA",
                release="master",
            ),
            OpenEdxApplicationVersion(
                application="ora-grading",
                application_type="MFE",
                release="sumac",
                branding_overrides=default_branding_overrides,
            ),
            OpenEdxApplicationVersion(
                application="xqueue",
                application_type="IDA",
                release="sumac",
            ),
            OpenEdxApplicationVersion(
                application="xqwatcher",
                application_type="IDA",
                branch_override="master",
                origin_override="https://github.com/mitodl/xqueue-watcher",
                release="sumac",
            ),
        ],
        "mitx-staging": [
            OpenEdxApplicationVersion(
                application="codejail",
                application_type="IDA",
                release="sumac",
            ),
            OpenEdxApplicationVersion(
                application="communications",
                application_type="MFE",
                release="sumac",
                branding_overrides=default_branding_overrides,
            ),
            OpenEdxApplicationVersion(
                application="authoring",
                application_type="MFE",
                release="sumac",
                branding_overrides=default_branding_overrides,
            ),
            OpenEdxApplicationVersion(
                application="discussions",
                application_type="MFE",
                release="sumac",
                branding_overrides=default_branding_overrides,
            ),
            OpenEdxApplicationVersion(
                application="edx-platform",
                application_type="IDA",
                release="sumac",
                branch_override="mitx/sumac",
                origin_override="https://github.com/mitodl/edx-platform",
                runtime_version_override="3.11",
            ),
            OpenEdxApplicationVersion(
                application="edxapp_theme",
                application_type="IDA",
                release="sumac",
                branch_override="sumac",
                origin_override="https://github.com/mitodl/mitx-theme",
            ),
            OpenEdxApplicationVersion(
                application="forum",
                application_type="IDA",
                release="sumac",
            ),
            OpenEdxApplicationVersion(
                application="gradebook",
                application_type="MFE",
                release="sumac",
                branding_overrides=default_branding_overrides,
            ),
            OpenEdxApplicationVersion(
                application="learning",
                application_type="MFE",
                release="sumac",
                branding_overrides=default_branding_overrides,
            ),
            OpenEdxApplicationVersion(
                application="learner-dashboard",
                application_type="MFE",
                release="sumac",
                branding_overrides={
                    "@edx/brand@npm": "@mitodl/brand-mitol-residential@latest",
                    **default_branding_overrides,
                },
            ),
            OpenEdxApplicationVersion(
                application="notes-api",
                application_type="IDA",
                release="master",
            ),
            OpenEdxApplicationVersion(
                application="ora-grading",
                application_type="MFE",
                release="sumac",
                branding_overrides=default_branding_overrides,
            ),
            OpenEdxApplicationVersion(
                application="xqueue",
                application_type="IDA",
                release="sumac",
            ),
            OpenEdxApplicationVersion(
                application="xqwatcher",
                application_type="IDA",
                branch_override="master",
                origin_override="https://github.com/mitodl/xqueue-watcher",
                release="sumac",
            ),
        ],
        "xpro": [
            OpenEdxApplicationVersion(
                application="codejail",
                application_type="IDA",
                release="sumac",
            ),
            OpenEdxApplicationVersion(
                application="authoring",
                application_type="MFE",
                release="sumac",
                branding_overrides=default_branding_overrides,
            ),
            OpenEdxApplicationVersion(
                application="discussions",
                application_type="MFE",
                release="sumac",
                branding_overrides=default_branding_overrides,
            ),
            OpenEdxApplicationVersion(
                application="edx-platform",
                application_type="IDA",
                release="sumac",
            ),
            OpenEdxApplicationVersion(
                application="edxapp_theme",
                application_type="IDA",
                release="sumac",
                branch_override="sumac",
                origin_override="https://github.com/mitodl/mitxpro-theme",
            ),
            OpenEdxApplicationVersion(
                application="forum",
                application_type="IDA",
                release="sumac",
            ),
            OpenEdxApplicationVersion(
                application="gradebook",
                application_type="MFE",
                release="sumac",
                branding_overrides=default_branding_overrides,
            ),
            OpenEdxApplicationVersion(
                application="learning",
                application_type="MFE",
                release="sumac",
                branding_overrides=default_branding_overrides,
            ),
            OpenEdxApplicationVersion(
                application="notes-api",
                application_type="IDA",
                release="master",
            ),
            OpenEdxApplicationVersion(
                application="ora-grading",
                application_type="MFE",
                release="sumac",
                branding_overrides=default_branding_overrides,
            ),
        ],
    },
    "master": {
        "mitxonline": [
            OpenEdxApplicationVersion(
                application="codejail",
                application_type="IDA",
                release="master",
            ),
            OpenEdxApplicationVersion(
                application="communications",
                application_type="MFE",
                release="master",
            ),
            OpenEdxApplicationVersion(
                application="authoring",
                application_type="MFE",
                release="master",
                branding_overrides=default_branding_overrides,
            ),
            OpenEdxApplicationVersion(
                application="discussions",
                application_type="MFE",
                release="master",
                branding_overrides=default_branding_overrides,
            ),
            OpenEdxApplicationVersion(
                application="edx-platform",
                application_type="IDA",
                release="master",
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
                branding_overrides=default_branding_overrides,
            ),
            OpenEdxApplicationVersion(
                application="learning",
                application_type="MFE",
                release="master",
                branding_overrides=default_branding_overrides,
                translation_overrides=[
                    "atlas pull -r mitodl/mitxonline-translations -n main translations/frontend-app-learning/src/i18n/messages:src/i18n/messages/frontend-app-learning",  # noqa: E501
                    "node_modules/@edx/frontend-platform/i18n/scripts/intl-imports.js frontend-app-learning",  # noqa: E501
                ],
            ),
            OpenEdxApplicationVersion(
                application="ora-grading",
                application_type="MFE",
                release="master",
                branding_overrides=default_branding_overrides,
            ),
            OpenEdxApplicationVersion(
                application="xqueue",
                application_type="IDA",
                release="master",
            ),
            OpenEdxApplicationVersion(
                application="xqwatcher",
                application_type="IDA",
                branch_override="master",
                origin_override="https://github.com/mitodl/xqueue-watcher",
                release="master",
            ),
        ],
    },
}
