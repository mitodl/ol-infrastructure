from functools import partial

from bridge.settings.openedx.types import (
    DeploymentEnvRelease,
    OpenEdxApplication,
    OpenEdxApplicationType,
    OpenEdxApplicationVersion,
    OpenEdxDeploymentName,
    OpenEdxMicroFrontend,
    OpenEdxSupportedRelease,
)
from bridge.settings.openedx.version_matrix import (
    OpenLearningOpenEdxDeployment,
    ReleaseMap,
)


def _fetch_application_version(
    release_map: dict[
        OpenEdxSupportedRelease,
        dict[OpenEdxDeploymentName, list[OpenEdxApplicationVersion]],
    ],
    release_name: OpenEdxSupportedRelease,
    deployment: OpenEdxDeploymentName,
    application_name: OpenEdxApplication | OpenEdxMicroFrontend,
) -> OpenEdxApplicationVersion | None:
    app_versions = release_map[release_name][deployment]
    fetched_app_version = None
    for app_version in app_versions:
        if app_version.application == application_name:
            fetched_app_version = app_version
    return fetched_app_version


def _fetch_applications_by_type(
    release_map: dict[
        OpenEdxSupportedRelease,
        dict[OpenEdxDeploymentName, list[OpenEdxApplicationVersion]],
    ],
    release_name: OpenEdxSupportedRelease,
    deployment: OpenEdxDeploymentName,
    application_type: OpenEdxApplicationType,
) -> list[OpenEdxApplicationVersion]:
    app_versions = release_map[release_name][deployment]
    fetched_app_versions = []
    for app_version in app_versions:
        if app_version.application_type == application_type:
            fetched_app_versions.append(app_version)  # noqa: PERF401
    return fetched_app_versions


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


def _filter_deployments_by_application(
    release_map: dict[
        OpenEdxSupportedRelease,
        dict[OpenEdxDeploymentName, list[OpenEdxApplicationVersion]],
    ],
    release_name: OpenEdxSupportedRelease,
    application_name: OpenEdxApplication,
) -> list[DeploymentEnvRelease]:
    filtered_deployments = []
    for deployment in OpenLearningOpenEdxDeployment:
        release_match = False
        for env_tuple in deployment.value.env_release_map:
            if release_name == env_tuple.edx_release:
                release_match = True
        if release_match:
            app_versions = release_map[release_name][deployment.value.deployment_name]
            for app_version in app_versions:
                if app_version.application == application_name:
                    filtered_deployments.append(deployment.value)  # noqa: PERF401
    return filtered_deployments


fetch_application_version = partial(_fetch_application_version, ReleaseMap)
fetch_applications_by_type = partial(_fetch_applications_by_type, ReleaseMap)
filter_deployments_by_application = partial(
    _filter_deployments_by_application, ReleaseMap
)
