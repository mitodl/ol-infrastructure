import sys
import textwrap
from collections import defaultdict
from itertools import chain, product
from typing import Literal, Optional

from pydantic import BaseModel

from bridge.settings.openedx.accessors import fetch_applications_by_type
from bridge.settings.openedx.types import (
    EnvStage,
    OpenEdxApplicationVersion,
    OpenEdxDeploymentName,
    OpenEdxMicroFrontend,
    OpenEdxSupportedRelease,
)
from bridge.settings.openedx.version_matrix import OpenLearningOpenEdxDeployment
from ol_concourse.lib.models.fragment import PipelineFragment
from ol_concourse.lib.models.pipeline import (
    AnonymousResource,
    Command,
    GetStep,
    Identifier,
    Input,
    Job,
    Output,
    Pipeline,
    Platform,
    PutStep,
    Resource,
    TaskConfig,
    TaskStep,
)
from ol_concourse.lib.resource_types import rclone
from ol_concourse.lib.resources import git_repo


class OpenEdxVars(BaseModel):
    about_us_url: Optional[str] = None
    accessibility_url: Optional[str] = None
    account_settings_url: Optional[str] = None
    contact_url: Optional[str] = None
    enable_certificate_page: Optional[Literal["true", "false"]] = None
    deployment_name: OpenEdxDeploymentName
    display_feedback_widget: Optional[str] = None
    environment: str
    environment_stage: EnvStage
    favicon_url: str
    honor_code_url: Optional[str] = None
    learning_base_url: Optional[str] = None
    lms_domain: str
    logo_url: str
    marketing_site_domain: str
    plugin_slot_config_file_map: Optional[dict[str, str]] = None
    privacy_policy_url: Optional[str] = None
    schedule_email_section: Optional[str] = None
    site_name: str
    studio_domain: str
    support_url: str
    terms_of_service_url: str
    trademark_text: Optional[str] = None
    logo_trademark_url: Optional[str] = None

    @property
    def release_name(self) -> OpenEdxSupportedRelease:
        return OpenLearningOpenEdxDeployment.get_item(
            self.deployment_name
        ).release_by_env(self.environment_stage)


def mfe_params(
    open_edx: OpenEdxVars, mfe: OpenEdxApplicationVersion
) -> dict[str, Optional[str]]:
    learning_mfe_path = OpenEdxMicroFrontend.learn.path
    discussion_mfe_path = OpenEdxMicroFrontend.discussion.path
    return {
        "ABOUT_US_URL": open_edx.about_us_url,
        "ACCESSIBILITY_URL": open_edx.accessibility_url,
        "ACCESS_TOKEN_COOKIE_NAME": (
            f"{open_edx.environment}-edx-jwt-cookie-header-payload"
        ),
        "ACCOUNT_SETTINGS_URL": open_edx.account_settings_url,
        "BASE_URL": f"https://{open_edx.lms_domain}/{mfe.application.path}",
        "CONTACT_URL": open_edx.contact_url,
        "CSRF_TOKEN_API_PATH": "/csrf/api/v1/token",
        "DISPLAY_FEEDBACK_WIDGET": open_edx.display_feedback_widget,
        "ENABLE_CERTIFICATE_PAGE": open_edx.enable_certificate_page,
        "DISCUSSIONS_MFE_BASE_URL": f"https://{open_edx.lms_domain}/{discussion_mfe_path}",
        "FAVICON_URL": open_edx.favicon_url,
        "HONOR_CODE_URL": open_edx.honor_code_url,
        "LANGUAGE_PREFERENCE_COOKIE_NAME": (
            f"{open_edx.environment}-open-edx-language-preference"
        ),
        "LMS_BASE_URL": f"https://{open_edx.lms_domain}",
        "LEARNING_BASE_URL": f"https://{open_edx.lms_domain}/{learning_mfe_path}",
        "LOGIN_URL": f"https://{open_edx.lms_domain}/login",
        "LOGOUT_URL": f"https://{open_edx.lms_domain}/logout",
        "LOGO_ALT_TEXT": None,
        "LOGO_TRADEMARK_URL": open_edx.logo_trademark_url or open_edx.logo_url,
        "LOGO_URL": open_edx.logo_url,
        "LOGO_WHITE_URL": open_edx.logo_url,
        "MARKETING_SITE_BASE_URL": f"https://{open_edx.marketing_site_domain}",
        "ORDER_HISTORY_URL": None,  # Intentionally left blank to turn off a menu entry
        "PRIVACY_POLICY_URL": open_edx.privacy_policy_url,
        "PUBLIC_PATH": f"/{mfe.application.path}/",
        "REFRESH_ACCESS_TOKEN_ENDPOINT": f"https://{open_edx.lms_domain}/login_refresh",
        "SCHEDULE_EMAIL_SECTION": open_edx.schedule_email_section,
        "SEARCH_CATALOG_URL": f"https://{open_edx.lms_domain}/courses",
        "SESSION_COOKIE_DOMAIN": open_edx.lms_domain,
        "SITE_NAME": open_edx.site_name,
        "STUDIO_BASE_URL": f"https://{open_edx.studio_domain}",
        "SUPPORT_URL": f"https://{open_edx.support_url}",
        "TERMS_OF_SERVICE_URL": open_edx.terms_of_service_url,
        "TRADEMARK_TEXT": open_edx.trademark_text,
        "USER_INFO_COOKIE_NAME": f"{open_edx.environment}-edx-user-info",
    }


def mfe_job(
    open_edx: OpenEdxVars,
    mfe: OpenEdxApplicationVersion,
    previous_job: Optional[Job] = None,
) -> PipelineFragment:
    mfe_name = mfe.application.value
    mfe_repo = git_repo(
        name=Identifier(f"mfe-app-{mfe_name}"),
        uri=mfe.git_origin,
        branch=mfe.release_branch,
    )
    mfe_configs = git_repo(
        name=Identifier("mfe-slots-config"),
        uri="https://github.com/mitodl/ol-infrastructure",
        paths=["src/bridge/settings/openedx/mfe/"],
        branch="mfe_plugin_slots_config",
    )

    clone_mfe_repo = GetStep(
        get=mfe_repo.name,
        trigger=previous_job is None and open_edx.environment_stage != "production",
    )
    clone_mfe_configs = GetStep(
        get=mfe_configs.name,
        trigger=previous_job is None and open_edx.environment_stage != "production",
    )
    branding_overrides = "\n".join(
        (
            f"npm install {component}:{override}"
            for component, override in (mfe.branding_overrides or {}).items()
        )
    )

    translation_overrides = "\n".join(cmd for cmd in mfe.translation_overrides or [])
    if previous_job and mfe_repo.name == previous_job.plan[0].get:
        clone_mfe_repo.passed = [previous_job.name]
        clone_mfe_configs.passed = [previous_job.name]

    mfe_build_dir = Output(name=Identifier("mfe-build"))
    mfe_setup_plan = [clone_mfe_repo]

    mfe_plugin_type_map = {
        "learning": "learning",
        "discussions": "learning",
        "ora-grading": "learning",
        "communications": "learning",
        "gradebook": "simple",
        "learner-dashboard": "simple",
    }
    if slot_config_file := (open_edx.plugin_slot_config_file_map or {}).get(
        mfe_plugin_type_map.get(OpenEdxMicroFrontend[mfe_name].value)  # type: ignore [arg-type]
    ):
        mfe_setup_command = textwrap.dedent(
            f"""\
                                cp -r {mfe_repo.name}/* {mfe_build_dir.name}
                                cp {mfe_configs.name}/src/bridge/settings/openedx/mfe/{slot_config_file} {mfe_build_dir.name}/env.config.jsx
                                """  # noqa: E501
        )
    else:
        mfe_setup_command = f"cp -r {mfe_repo.name}/* {mfe_build_dir.name}"

    mfe_setup_plan += [
        clone_mfe_configs,
        TaskStep(
            task=Identifier("merge-mfe-and-configs"),
            config=TaskConfig(
                platform=Platform.linux,
                image_resource=AnonymousResource(
                    type="registry-image",
                    source={"repository": "debian", "tag": "bookworm-slim"},
                ),
                inputs=[Input(name=mfe_repo.name), Input(name=mfe_configs.name)],
                outputs=[mfe_build_dir],
                run=Command(
                    path="sh",
                    args=["-exc", mfe_setup_command],
                ),
            ),
        ),
    ]

    mfe_build_plan = [
        TaskStep(
            task=Identifier("compile-and-deploy-mfe"),
            config=TaskConfig(
                platform=Platform.linux,
                image_resource=AnonymousResource(
                    type="registry-image",
                    source={
                        "repository": "node",
                        "tag": f"{mfe.runtime_version}-bookworm-slim",
                    },
                ),
                inputs=[Input(name=mfe_build_dir.name)],
                outputs=[
                    Output(
                        name=Identifier("compiled-mfe"),
                        path=f"{mfe_build_dir.name}/dist",
                    )
                ],
                params=mfe_params(open_edx, mfe),
                run=Command(
                    path="sh",
                    dir=mfe_build_dir.name,
                    args=[
                        "-exc",
                        # Ensure that webpack is installed (TMM 2023-06-27)
                        textwrap.dedent(
                            f"""\
                                apt-get update
                                apt-get install -q -y python3 python-is-python3 build-essential git
                                npm install
                                npm install -g @edx/openedx-atlas
                                {branding_overrides}
                                {translation_overrides}
                                npm install webpack
                                NODE_ENV=production npm run build
                                """  # noqa: E501
                        ),
                    ],
                ),
            ),
        ),
        PutStep(
            put="mfe-app-bucket",
            params={
                "source": "compiled-mfe",
                "destination": [
                    {
                        "command": "sync",
                        "dir": f"s3-remote:{open_edx.environment}-edxapp-mfe/{mfe.application.path}/",  # noqa: E501
                    }
                ],
            },
        ),
    ]

    mfe_job_definition = Job(
        name=Identifier(f"compile-and-deploy-mfe-{mfe_name}-to-{open_edx.environment}"),
        plan=mfe_setup_plan + mfe_build_plan,
    )
    return PipelineFragment(
        resources=[mfe_repo, mfe_configs], jobs=[mfe_job_definition]
    )


def mfe_pipeline(
    deployment_name: OpenEdxDeploymentName, release_name: OpenEdxSupportedRelease
) -> Pipeline:
    deployment = OpenLearningOpenEdxDeployment.get_item(deployment_name)
    mfes = fetch_applications_by_type(release_name, deployment_name, "MFE")
    fragments: dict[str, list[PipelineFragment]] = defaultdict(list)
    deploy_envs = deployment.envs_by_release(release_name)
    edx_vars = [
        edx_var
        for edx_var in deployments[deployment_name]
        if edx_var.environment_stage in deploy_envs
    ]
    for edx_var, mfe in product(edx_vars, mfes):
        try:
            prev_job = fragments.get(mfe.application, [])[-1].jobs[0]
        except IndexError:
            prev_job = None
        mfe_fragment = mfe_job(edx_var, mfe, prev_job)
        fragments[mfe.application].append(mfe_fragment)
    combined_fragments = PipelineFragment.combine_fragments(
        *chain.from_iterable(fragments.values()),
    )
    return Pipeline(
        resource_types=[rclone(), *combined_fragments.resource_types],
        resources=[
            Resource(
                name=Identifier("mfe-app-bucket"),
                type="rclone",
                source={
                    "config": textwrap.dedent(
                        "                        [s3-remote]\n                        type = s3\n                        provider = AWS\n                        env_auth = true\n                        region = us-east-1\n                        "  # noqa: E501
                    )
                },
            ),
            *combined_fragments.resources,
        ],
        jobs=combined_fragments.jobs,
    )


if __name__ == "__main__":
    from ol_concourse.pipelines.open_edx.mfe.values import deployments

    deployment: OpenEdxDeploymentName = sys.argv[1]
    release_name: OpenEdxSupportedRelease = sys.argv[2]
    pipeline = mfe_pipeline(deployment, release_name)
    with open("definition.json", "w") as definition:  # noqa: PTH123
        definition.write(pipeline.model_dump_json(indent=2))
    sys.stdout.write(pipeline.model_dump_json(indent=2))
