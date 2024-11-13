import sys

from bridge.settings.openedx.types import OpenEdxSupportedRelease
from ol_concourse.lib.models.pipeline import (
    AnonymousResource,
    Command,
    GetStep,
    Identifier,
    Input,
    Job,
    LoadVarStep,
    Output,
    Pipeline,
    Platform,
    SetPipelineStep,
    TaskConfig,
    TaskStep,
)
from ol_concourse.lib.resources import git_repo

pipeline_code = git_repo(
    name=Identifier("eks-cluster-pipeline-code"),
    uri="https://github.com/mitodl/ol-infrastructure",
    paths=[
        "src/bridge/settings/openedx/",
        "src/ol_concourse/lib/",
        "src/ol_concourse/pipelines/infrastructure/eks_cluster",
    ],
)


def build_meta_job(release_name: str):
    if release_name == "meta":
        pipeline_definition_path = (
            "src/ol_concourse/pipelines/infrastructure/eks_cluster/meta.py"
        )
        pipeline_team = "main"
        pipeline_id = "self"
    else:
        pipeline_definition_path = (
            "src/ol_concourse/pipelines/open_edx/eks_cluster/pulumi_pipeline.py"
        )
        pipeline_team = "infrastructure"
        pipeline_id = f"pulumi-eks-cluster-{release_name}"
    return Job(
        name=Identifier(f"create-eks-cluster-{release_name}-pipeline"),
        plan=[
            # TaskStep to generate list of pulumi eks_cluster projects
            GetStep(get=pipeline_code.name, trigger=True),
            TaskStep(
                task=Identifier("generate-eks-cluster-list"),
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
                    outputs=[Output(name=Identifier("eks-cluster-list"))],
                    run=Command(
                        path="sh",
                        dir=pipeline_code.name,
                        user="root",
                        args=[
                            "-c",
                            f"ls ${pipeline_code.name}/src/ol_concourse/pipelines/infrastructure/eks_cluster",  # noqa: E501
                        ],
                    ),
                ),
            ),
            LoadVarStep(
                file="eks-cluster-list/eks-cluster-list.yml",
                vars=Identifier("eks_cluster_projects"),
            ),
            # LoadVars to load the YAML/JSON into the meta_jobs variable.
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
        "\nfly -t <target> set-pipeline -p docker-packer-pulumi-eks-cluster-meta -c definition.json"  # noqa: E501
    )
