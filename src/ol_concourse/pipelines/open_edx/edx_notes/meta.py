import sys

from bridge.settings.openedx.types import OpenEdxSupportedRelease
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

pipeline_code = git_repo(
    name=Identifier("notes-pipeline-code"),
    uri="https://github.com/mitodl/ol-infrastructure",
    paths=[
        "src/ol_concourse/lib/",
        "src/ol_concourse/pipelines/open_edx/edx_notes/",
        "src/bridge/settings/openedx/",
    ],
)


def build_meta_job(release_name):
    if release_name == "meta":
        pipeline_definition_path = (
            "src/ol_concourse/pipelines/open_edx/edx_notes/meta.py"
        )
        pipeline_team = "main"
        pipeline_id = "self"
    else:
        pipeline_definition_path = (
            "src/ol_concourse/pipelines/open_edx/edx_notes/docker_pulumi_pipeline.py"
        )
        pipeline_team = "infrastructure"
        pipeline_id = f"docker-pulumi-edx-notes-{release_name}"
    return Job(
        name=Identifier(f"create-edx-notes-{release_name}-pipeline"),
        plan=[
            GetStep(get=pipeline_code.name, trigger=True),
            TaskStep(
                task=Identifier(f"generate-{release_name}-pipeline-definition"),
                config=TaskConfig(
                    platform=Platform.linux,
                    image_resource=AnonymousResource(
                        type="registry-image",
                        source={
                            "repository": "mitodl/ol-infrastructure",
                            "tag": "latest",
                        },
                    ),
                    inputs=[Input(name=Identifier(pipeline_code.name))],
                    outputs=[Output(name=Identifier("pipeline"))],
                    run=Command(
                        path="python",
                        dir="pipeline",
                        user="root",
                        args=[
                            f"../{pipeline_code.name}/{pipeline_definition_path}",
                            release_name,
                        ],
                    ),
                ),
            ),
            SetPipelineStep(
                team=pipeline_team,
                set_pipeline=Identifier(pipeline_id),
                file="pipeline/definition.json",
            ),
        ],
    )


meta_jobs = [build_meta_job(release_name) for release_name in OpenEdxSupportedRelease]
meta_jobs.append(build_meta_job("meta"))

meta_pipeline = Pipeline(resources=[pipeline_code], jobs=meta_jobs)


if __name__ == "__main__":
    with open("definition.json", "w") as definition:  # noqa: PTH123
        definition.write(meta_pipeline.model_dump_json(indent=2))
    sys.stdout.write(meta_pipeline.model_dump_json(indent=2))
    sys.stdout.write(
        "\nfly -t <target> set-pipeline -p docker-pulumi-edx-notes-meta -c definition.json"  # noqa: E501
    )
