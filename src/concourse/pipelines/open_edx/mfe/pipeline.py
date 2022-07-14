import textwrap
from typing import Optional

from pydantic import BaseModel

from concourse.lib.models import (
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
from concourse.lib.resource_types import rclone
from concourse.lib.resources import git_repo


class MFEAppVars(BaseModel):
    node_major_version: int
    path: str
    repository: str


class OpenEdxVars(BaseModel):
    contact_url: Optional[str]
    environment: str
    favicon_url: str
    honor_code_url: Optional[str]
    lms_domain: str
    logo_url: str
    marketing_site_domain: str
    release_name: str
    site_name: str
    studio_domain: str
    support_url: str
    terms_of_service_url: str
    trademark_text: Optional[str]


def mfe_params(open_edx: OpenEdxVars, mfe: MFEAppVars) -> dict[str, Optional[str]]:
    return {
        "ACCESS_TOKEN_COOKIE_NAME": f"{open_edx.environment}-edx-jwt-cookie-header-payload",
        "BASE_URL": f"https://{open_edx.lms_domain}",
        "CSRF_TOKEN_API_PATH": "/csrf/api/v1/token",
        "CONTACT_URL": open_edx.contact_url,
        "FAVICON_URL": open_edx.favicon_url,
        "HONOR_CODE_URL": open_edx.honor_code_url,
        "LANGUAGE_PREFERENCE_COOKIE_NAME": f"{open_edx.environment}-open-edx-language-preference",
        "LMS_BASE_URL": f"https://{open_edx.lms_domain}",
        "LOGIN_URL": f"https://{open_edx.lms_domain}/login",
        "LOGOUT_URL": f"https://{open_edx.lms_domain}/logout",
        "LOGO_ALT_TEXT": None,
        "LOGO_TRADEMARK_URL": open_edx.logo_url,
        "LOGO_URL": open_edx.logo_url,
        "LOGO_WHITE_URL": open_edx.logo_url,
        "MARKETING_SITE_BASE_URL": f"https://{open_edx.marketing_site_domain}",
        "ORDER_HISTORY_URL": None,  # Intentionally left blank to turn off a menu entry
        "PUBLIC_PATH": f"/{mfe.path}/",
        "REFRESH_ACCESS_TOKEN_ENDPOINT": f"https://{open_edx.lms_domain}/login_refresh",
        "SEARCH_CATALOG_URL": f"https://{open_edx.lms_domain}/courses",
        "SESSION_COOKIE_DOMAIN": open_edx.lms_domain,
        "SITE_NAME": open_edx.site_name,
        "STUDIO_BASE_URL": f"https://{open_edx.studio_domain}",
        "SUPPORT_URL": f"https://{open_edx.support_url}",
        "TERMS_OF_SERVICE_URL": open_edx.terms_of_service_url,
        "TRADEMARK_TEXT": open_edx.trademark_text,
        "USER_INFO_COOKIE_NAME": f"{open_edx.environment}-edx-user-info",
    }


def mfe_job(open_edx: OpenEdxVars, mfe: MFEAppVars, previous_job: str = None) -> Job:
    mfe_dir = f"mfe-app-{mfe.path}"
    clone_git_repo = GetStep(
        get=f"mfe-app-{mfe.path}",
        trigger=previous_job is None,
    )
    if previous_job:
        clone_git_repo.passed = [previous_job]
    return Job(
        name=Identifier(f"compile-and-deploy-mfe-{mfe.path}-to-{open_edx.environment}"),
        plan=[
            clone_git_repo,
            TaskStep(
                task=Identifier("compile-and-deploy-mfe"),
                config=TaskConfig(
                    platform=Platform.linux,
                    image_resource=AnonymousResource(
                        type="registry-image",
                        source={
                            "repository": "node",
                            "tag": f"{mfe.node_major_version}-bullseye-slim",
                        },
                    ),
                    inputs=[Input(name=Identifier(mfe_dir))],
                    outputs=[
                        Output(name=Identifier("compiled-mfe"), path=f"{mfe_dir}/dist")
                    ],
                    params=mfe_params(open_edx, mfe),
                    run=Command(
                        path="sh",
                        dir=mfe_dir,
                        args=[
                            "-exc",
                            textwrap.dedent(
                                """\
                                apt-get update
                                apt-get install -q -y python build-essential
                                npm install
                                npm install @edx/frontend-component-footer@npm:@mitodl/frontend-component-footer-mitol@latest --legacy-peer-deps
                                npm install @edx/frontend-component-header@npm:@mitodl/frontend-component-header-mitol@latest --legacy-peer-deps
                                NODE_ENV=production npm run build
                                """
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
                            "dir": f"s3-remote:{open_edx.environment}-edxapp-mfe/{mfe.path}/",
                        }
                    ],
                },
            ),
        ],
    )


def mfe_pipeline(open_edx_envs: list[OpenEdxVars], mfe: MFEAppVars) -> Pipeline:
    jobs_list: list[Job] = []
    for edx_env in open_edx_envs:
        try:
            prev_job = jobs_list[-1].name
        except IndexError:
            prev_job = None
        jobs_list.append(mfe_job(edx_env, mfe, prev_job))
    return Pipeline(
        resource_types=[rclone()],
        resources=[
            git_repo(
                name=Identifier(f"mfe-app-{mfe.path}"),
                uri=mfe.repository,
                branch=open_edx_envs[0].release_name,
            ),
            Resource(
                name=Identifier("mfe-app-bucket"),
                type="rclone",
                source={
                    "config": textwrap.dedent(
                        """\
                    [s3-remote]
                    type = s3
                    provider = AWS
                    env_auth = true
                    region = us-east-1
                    """
                    )
                },
            ),
        ],
        jobs=jobs_list,
    )


if __name__ == "__main__":
    import sys

    from concourse.pipelines.open_edx.mfe.values import apps, deployments

    deployment = sys.argv[1]
    app = sys.argv[2]
    pipeline = mfe_pipeline(deployments[deployment], apps[app])
    with open("definition.json", "wt") as definition:
        definition.write(pipeline.json(indent=2))
    sys.stdout.write(pipeline.json(indent=2))
