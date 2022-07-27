import itertools

from concourse.lib.models.pipeline import (
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
from concourse.lib.resources import git_repo
from concourse.pipelines.open_edx.mfe.pipeline import MFEAppVars, OpenEdxVars
from concourse.pipelines.open_edx.mfe.values import apps, deployments


def meta_job(
    open_edx_deployment: str,
    mfe_app_name: str,
    open_edx_environments: list[OpenEdxVars],
    mfe_vars: MFEAppVars,
) -> Job:
    return Job(
        name=Identifier(f"create-{open_edx_deployment}-{mfe_app_name}-mfe-pipeline"),
        plan=[
            GetStep(
                get="mfe-pipeline-definitions",
                trigger=True,
            ),
            TaskStep(
                task=Identifier(
                    f"generate-{open_edx_deployment}-{mfe_app_name}-mfe-pipeline-file"
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
                            "../mfe-pipeline-definitions/src/concourse/pipelines/open_edx/mfe/pipeline.py",
                            open_edx_deployment,
                            mfe_app_name,
                        ],
                    ),
                ),
            ),
            SetPipelineStep(
                team=Identifier(open_edx_deployment),
                set_pipeline=Identifier(
                    f"{open_edx_deployment}-{mfe_app_name}-mfe-pipeline"
                ),
                file="pipeline/definition.json",
            ),
        ],
    )


def meta_pipeline() -> Pipeline:
    combinations = itertools.product(deployments.keys(), apps.keys())
    mfe_definitions = git_repo(
        name=Identifier("mfe-pipeline-definitions"),
        uri="https://github.com/mitodl/ol-infrastructure",
        branch="main",
    )
    mfe_definitions.source["paths"] = [
        "src/concourse/pipelines/open_edx/mfe/",
        "src/concourse/lib/",
    ]
    pipeline_jobs = [
        meta_job(deployment, app, deployments[deployment], apps[app])
        for deployment, app in combinations
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
                    task=Identifier(f"generate-mfe-meta-pipeline-file"),
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
                                "../mfe-pipeline-definitions/src/concourse/pipelines/open_edx/mfe/meta.py",
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

    with open("definition.json", "wt") as definition:
        definition.write(meta_pipeline().json(indent=2))
    sys.stdout.write(meta_pipeline().json(indent=2))
