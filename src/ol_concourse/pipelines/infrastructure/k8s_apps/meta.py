import sys

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


def meta_job(app_name: str) -> Job:
    return Job(
        name=Identifier(f"create-{app_name}-pipeline"),
        plan=[
            GetStep(
                get="k8s-app-pipeline-definitions",
                trigger=True,
            ),
            TaskStep(
                task=Identifier(f"generate-{app_name}-pipeline-file"),
                config=TaskConfig(
                    platform=Platform.linux,
                    image_resource=AnonymousResource(
                        type="registry-image",
                        source={
                            "repository": "mitodl/ol-infrastructure",
                            "tag": "latest",
                        },
                    ),
                    inputs=[Input(name=Identifier("k8s-app-pipeline-definitions"))],
                    outputs=[Output(name=Identifier("pipeline"))],
                    run=Command(
                        path="python",
                        dir="pipeline",
                        user="root",
                        args=[
                            "../k8s-app-pipeline-definitions/src/ol_concourse/pipelines/infrastructure/k8s_apps/docker_pulumi.py",
                            app_name,
                        ],
                    ),
                ),
            ),
            SetPipelineStep(
                set_pipeline=Identifier(f"{app_name}-pipeline"),
                file="pipeline/definition.json",
            ),
        ],
    )


def meta_pipeline(app_names: list[str]) -> Pipeline:
    pipeline_definitions = git_repo(
        name=Identifier("k8s-app-pipeline-definitions"),
        uri="https://github.com/mitodl/ol-infrastructure",
        branch="main",
        paths=[
            "src/ol_concourse/pipelines/infrastructure/k8s_apps/",
            "src/ol_concourse/lib/",
            "src/ol_concourse/pipelines/constants.py",
        ],
    )
    pipeline_jobs = [meta_job(app_name) for app_name in app_names]
    pipeline_jobs.append(
        Job(
            name=Identifier("set-k8s-app-meta-pipeline"),
            plan=[
                GetStep(
                    get="k8s-app-pipeline-definitions",
                    trigger=True,
                ),
                TaskStep(
                    task=Identifier(f"generate-k8s-app-meta-pipeline-file"),  # noqa: F541
                    config=TaskConfig(
                        platform=Platform.linux,
                        image_resource=AnonymousResource(
                            type="registry-image",
                            source={
                                "repository": "mitodl/ol-infrastructure",
                                "tag": "latest",
                            },
                        ),
                        inputs=[Input(name=Identifier("k8s-app-pipeline-definitions"))],
                        outputs=[Output(name=Identifier("pipeline"))],
                        run=Command(
                            path="python",
                            dir="pipeline",
                            user="root",
                            args=[
                                "../k8s-app-pipeline-definitions/src/ol_concourse/pipelines/infrastructure/k8s_apps/meta.py",
                                repr(app_names),
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
        resources=[pipeline_definitions],
        jobs=pipeline_jobs,
    )


if __name__ == "__main__":
    app_names = [
        "learn-ai",
        "mit-learn",
        "mitxonline",
        "mit-learn-nextjs",
        "xpro",
        "ocw-studio",
    ]

    with open("definition.json", "w") as definition:  # noqa: PTH123
        definition.write(meta_pipeline(app_names).model_dump_json(indent=2))
    sys.stdout.write(meta_pipeline(app_names).model_dump_json(indent=2))
