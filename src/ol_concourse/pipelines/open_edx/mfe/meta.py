from bridge.settings.openedx.types import OpenEdxSupportedRelease
from bridge.settings.openedx.version_matrix import OpenLearningOpenEdxDeployment
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
                            "repository": "mitodl/ol-infrastructure",
                            "tag": "latest",
                        },
                    ),
                    inputs=[Input(name=Identifier("mfe-pipeline-definitions"))],
                    outputs=[Output(name=Identifier("pipeline"))],
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


def meta_pipeline() -> Pipeline:
    pipeline_jobs = []
    mfe_definitions = git_repo(
        name=Identifier("mfe-pipeline-definitions"),
        uri="https://github.com/mitodl/ol-infrastructure",
        branch="main",
        paths=[
            "src/ol_concourse/pipelines/open_edx/mfe/",
            "src/ol_concourse/lib/",
            "src/bridge/settings/openedx/",
        ],
    )
    pipeline_jobs = [
        meta_job(deployment, release)
        for deployment in deployments
        for release in OpenLearningOpenEdxDeployment.get_item(deployment).releases
    ]
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
                                "repository": "mitodl/ol-infrastructure",
                                "tag": "latest",
                            },
                        ),
                        inputs=[Input(name=Identifier("mfe-pipeline-definitions"))],
                        outputs=[Output(name=Identifier("pipeline"))],
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

    with open("definition.json", "w") as definition:  # noqa: PTH123
        definition.write(meta_pipeline().model_dump_json(indent=2))
    sys.stdout.write(meta_pipeline().model_dump_json(indent=2))
