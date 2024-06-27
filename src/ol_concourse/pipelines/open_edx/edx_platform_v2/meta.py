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

pipeline_code = git_repo(
    name=Identifier("edxapp-pipeline-code"),
    uri="https://github.com/mitodl/ol-infrastructure",
    branch="main",
)


def build_meta_job(pipeline_name: str):
    if pipeline_name == "meta":
        pipeline_definition_path = (
            "src/ol_concourse/pipelines/open_edx/edx_platform_v2/meta.py"
        )
        pipeline_team = "main"
        pipeline_id = "self"
    else:
        pipeline_definition_path = "src/ol_concourse/pipelines/open_edx/edx_platform_v2/earthly_packer_pulumi_pipeline.py"  # noqa: E501
        pipeline_team = "infrastructure"
        pipeline_id = f"docker-packer-pulumi-edxapp-{pipeline_name}"
    return Job(
        name=Identifier(f"create-edxapp-{pipeline_name}-earthly-pipeline"),
        plan=[
            GetStep(get=pipeline_code.name, trigger=True),
            TaskStep(
                task=Identifier(f"generate-{pipeline_name}-pipeline-definition"),
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


meta_jobs = [build_meta_job("meta"), build_meta_job("global")]

meta_pipeline = Pipeline(resources=[pipeline_code], jobs=meta_jobs)


if __name__ == "__main__":
    with open("definition.json", "w") as definition:  # noqa: PTH123
        definition.write(meta_pipeline.json(indent=2))
    sys.stdout.write(meta_pipeline.json(indent=2))
    sys.stdout.write(
        "\nfly -t <target> set-pipeline -p docker-packer-pulumi-edxapp-meta -c definition.json"  # noqa: E501
    )
