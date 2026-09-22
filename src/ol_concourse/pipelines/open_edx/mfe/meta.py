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
    SetPipelineStep,
    TaskConfig,
    TaskStep,
)
from ol_concourse.lib.resources import git_repo

from bridge.settings.openedx.types import OpenEdxSupportedRelease
from bridge.settings.openedx.version_matrix import OpenLearningOpenEdxDeployment
from ol_concourse.pipelines.constants import ECR_REGION, dockerhub_ecr_image_uri
from ol_concourse.pipelines.open_edx.mfe.site_pipeline import SITE_PROJECTS
from ol_concourse.pipelines.open_edx.mfe.values import deployments


def meta_job(
    open_edx_deployment: OpenLearningOpenEdxDeployment,
    open_edx_release: OpenEdxSupportedRelease,
) -> Job:
    return Job(
        name=Identifier(
            f"create-{open_edx_deployment}-{open_edx_release}-mfe-pipeline"
        ),
        plan=[
            GetStep(
                get="mfe-pipeline-definitions",
                trigger=True,
            ),
            TaskStep(
                task=Identifier(
                    f"generate-{open_edx_deployment}-{open_edx_release}-mfe-pipeline-file"
                ),
                config=TaskConfig(
                    platform=Platform.linux,
                    image_resource=AnonymousResource(
                        type="registry-image",
                        source={
                            "repository": dockerhub_ecr_image_uri(
                                "mitodl/ol-infrastructure"
                            ),
                            "tag": "latest",
                            "aws_region": ECR_REGION,
                        },
                    ),
                    inputs=[Input(name=Identifier("mfe-pipeline-definitions"))],
                    outputs=[Output(name=Identifier("pipeline"))],
                    params={"PYTHONPATH": "../mfe-pipeline-definitions/src"},
                    run=Command(
                        path="python",
                        dir="pipeline",
                        user="root",
                        args=[
                            "../mfe-pipeline-definitions/src/ol_concourse/pipelines/open_edx/mfe/pipeline.py",
                            open_edx_deployment,
                            open_edx_release,
                        ],
                    ),
                ),
            ),
            SetPipelineStep(
                team=Identifier(open_edx_deployment),
                set_pipeline=Identifier(
                    f"{open_edx_deployment}-{open_edx_release}-mfe-pipeline"
                ),
                file="pipeline/definition.json",
            ),
        ],
    )


def site_meta_job(deployment_name: str) -> Job:
    """Generate a meta job that creates the Site Project pipeline for one deployment."""
    return Job(
        name=Identifier(f"create-{deployment_name}-site-pipeline"),
        plan=[
            GetStep(
                get="mfe-pipeline-definitions",
                trigger=True,
            ),
            TaskStep(
                task=Identifier(f"generate-{deployment_name}-site-pipeline-file"),
                config=TaskConfig(
                    platform=Platform.linux,
                    image_resource=AnonymousResource(
                        type="registry-image",
                        source={
                            "repository": dockerhub_ecr_image_uri(
                                "mitodl/ol-infrastructure"
                            ),
                            "tag": "latest",
                            "aws_region": ECR_REGION,
                        },
                    ),
                    inputs=[Input(name=Identifier("mfe-pipeline-definitions"))],
                    outputs=[Output(name=Identifier("pipeline"))],
                    run=Command(
                        path="python",
                        dir="pipeline",
                        user="root",
                        args=[
                            "../mfe-pipeline-definitions/src/ol_concourse/pipelines/open_edx/mfe/site_pipeline.py",
                            deployment_name,
                        ],
                    ),
                ),
            ),
            SetPipelineStep(
                team=Identifier(deployment_name),
                set_pipeline=Identifier(f"{deployment_name}-site-pipeline"),
                file="pipeline/definition.json",
            ),
        ],
    )


def meta_pipeline() -> Pipeline:
    pipeline_jobs = []
    mfe_definitions = git_repo(
        name=Identifier("mfe-pipeline-definitions"),
        uri="https://github.com/mitodl/ol-infrastructure",
        branch="main",
        paths=[
            "src/ol_concourse/pipelines/open_edx/mfe/",
            "pyproject.toml",
            "src/ol_concourse/pipelines/constants.py",
            "src/ol_concourse/pipelines/jobs.py",
            "src/bridge/settings/openedx/",
        ],
    )
    pipeline_jobs = [
        meta_job(deployment, release)
        for deployment in deployments
        for release in OpenLearningOpenEdxDeployment.get_item(deployment).releases
    ]
    pipeline_jobs.extend(
        site_meta_job(project.deployment_name) for project in SITE_PROJECTS
    )
    pipeline_jobs.append(
        Job(
            name=Identifier("set-mfe-meta-pipeline"),
            plan=[
                GetStep(
                    get="mfe-pipeline-definitions",
                    trigger=True,
                ),
                TaskStep(
                    task=Identifier(f"generate-mfe-meta-pipeline-file"),  # noqa: F541
                    config=TaskConfig(
                        platform=Platform.linux,
                        image_resource=AnonymousResource(
                            type="registry-image",
                            source={
                                "repository": dockerhub_ecr_image_uri(
                                    "mitodl/ol-infrastructure"
                                ),
                                "tag": "latest",
                                "aws_region": ECR_REGION,
                            },
                        ),
                        inputs=[Input(name=Identifier("mfe-pipeline-definitions"))],
                        outputs=[Output(name=Identifier("pipeline"))],
                        params={"PYTHONPATH": "../mfe-pipeline-definitions/src"},
                        run=Command(
                            path="python",
                            dir="pipeline",
                            user="root",
                            args=[
                                "../mfe-pipeline-definitions/src/ol_concourse/pipelines/open_edx/mfe/meta.py",
                            ],
                        ),
                    ),
                ),
                SetPipelineStep(
                    set_pipeline="self",
                    file="pipeline/definition.json",
                ),
            ],
        )
    )
    return Pipeline(
        resources=[mfe_definitions],
        jobs=pipeline_jobs,
    )


if __name__ == "__main__":
    import sys

    from ol_concourse.pipelines.pipeline_output import pipeline_json_with_user_data

    release_names = sorted(
        {
            release
            for deployment in deployments
            for release in OpenLearningOpenEdxDeployment.get_item(deployment).releases
        }
    )
    output = pipeline_json_with_user_data(
        meta_pipeline(),
        user_data={
            "description": (
                "Self-updating registry for the Open edX MFE pipeline family: "
                f"one `{{deployment}}-{{release}}-mfe-pipeline` per (deployment, "
                f"release) pair across {', '.join(deployments)} "
                f"({', '.join(release_names)}), plus one "
                "`{deployment}-site-pipeline` per OEP-65 Site Project, all "
                "re-generated from `pipeline.py` and `site_pipeline.py` "
                "whenever this repo changes."
            ),
            "team": "main",
            "category": "meta",
        },
    )
    with open("definition.json", "w") as definition:  # noqa: PTH123
        definition.write(output)
    sys.stdout.write(output)
