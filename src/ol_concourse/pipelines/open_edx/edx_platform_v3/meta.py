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

from ol_concourse.pipelines.constants import ECR_REGION, dockerhub_ecr_image_uri
from ol_concourse.pipelines.pipeline_output import pipeline_json_with_user_data

pipeline_code = git_repo(
    name=Identifier("edxapp-pipeline-code"),
    uri="https://github.com/mitodl/ol-infrastructure",
    branch="main",
)


def build_meta_job(pipeline_name: str):
    if pipeline_name == "meta":
        pipeline_definition_path = (
            "src/ol_concourse/pipelines/open_edx/edx_platform_v3/meta.py"
        )
        pipeline_team = "main"
        pipeline_id = "self"
    else:
        pipeline_definition_path = (
            "src/ol_concourse/pipelines/open_edx/edx_platform_v3/pipeline.py"
        )
        pipeline_team = "infrastructure"
        pipeline_id = f"dagger-pulumi-edxapp-{pipeline_name}"
    return Job(
        name=Identifier(f"create-edxapp-{pipeline_name}-dagger-pipeline"),
        plan=[
            GetStep(get=pipeline_code.name, trigger=True),
            TaskStep(
                task=Identifier(f"generate-{pipeline_name}-pipeline-definition"),
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
                    inputs=[Input(name=Identifier(pipeline_code.name))],
                    outputs=[Output(name=Identifier("pipeline"))],
                    params={"PYTHONPATH": f"../{pipeline_code.name}/src"},
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
    pipeline_json = pipeline_json_with_user_data(
        meta_pipeline,
        user_data={
            "description": (
                "Regenerates and applies the `dagger-pulumi-edxapp-global` "
                "pipeline (and re-applies itself) via `fly set-pipeline`, keeping "
                "them in sync with `edx_platform_v3/pipeline.py` and this file."
            ),
            "team": "main",
            "category": "meta",
        },
    )
    with open("definition.json", "w") as definition:  # noqa: PTH123
        definition.write(pipeline_json)
    sys.stdout.write(pipeline_json)
    sys.stdout.write(
        "\nfly -t <target> set-pipeline -p dagger-pulumi-edxapp-meta-v3 -c definition.json"  # noqa: E501
    )
