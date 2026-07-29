"""Meta pipeline for managing dedicated Pulumi infrastructure pipelines.

Each entry in PIPELINE_CONFIGS maps a Concourse pipeline name to the relative
path of its definition script within the ol-infrastructure repository.  The
meta pipeline generates and registers each managed pipeline whenever the
relevant pipeline definition file changes, and keeps itself up-to-date via a
``set_pipeline: self`` job.

Fly command to bootstrap this meta pipeline:
    python meta.py
    fly -t pr-inf sp -p pulumi-infrastructure-meta -c definition.json
"""

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

_OL_INFRA_IMAGE_SOURCE = {
    "repository": dockerhub_ecr_image_uri("mitodl/ol-infrastructure"),
    "tag": "latest",
    "aws_region": ECR_REGION,
}
PIPELINE_CONFIGS: list[tuple[str, str]] = [
    (
        "pulumi-aws",
        "src/ol_concourse/pipelines/infrastructure/aws/pipeline.py",
    ),
    (
        "packer-pulumi-concourse",
        "src/ol_concourse/pipelines/infrastructure/concourse/pipeline.py",
    ),
    (
        "packer-pulumi-consul",
        "src/ol_concourse/pipelines/infrastructure/consul/pipeline.py",
    ),
    (
        "docker-pulumi-dagster",
        "src/ol_concourse/pipelines/infrastructure/dagster/pipeline.py",
    ),
    (
        "pulumi-eks-cluster",
        "src/ol_concourse/pipelines/infrastructure/eks_clusters/pipeline.py",
    ),
    (
        "pulumi-jupyterhub",
        "src/ol_concourse/pipelines/infrastructure/jupyterhub/pipeline.py",
    ),
    (
        "docker-pulumi-keycloak",
        "src/ol_concourse/pipelines/infrastructure/keycloak/pipeline.py",
    ),
    (
        "pulumi-kubewatch",
        "src/ol_concourse/pipelines/infrastructure/kubewatch/pipeline.py",
    ),
    (
        "pulumi-omnigraph",
        "src/ol_concourse/pipelines/infrastructure/omnigraph/pipeline.py",
    ),
    (
        "pulumi-witan",
        "src/ol_concourse/pipelines/infrastructure/witan/pipeline.py",
    ),
    (
        "docker-pulumi-superset",
        "src/ol_concourse/pipelines/infrastructure/superset/pipeline.py",
    ),
    (
        "packer-pulumi-vault",
        "src/ol_concourse/pipelines/infrastructure/vault/pipeline.py",
    ),
]


def meta_job(pipeline_name: str, script_path: str) -> Job:
    """Generate a job that creates/updates a single infrastructure pipeline."""
    return Job(
        name=Identifier(f"create-{pipeline_name}"),
        plan=[
            GetStep(
                get="infrastructure-pipeline-definitions",
                trigger=True,
            ),
            TaskStep(
                task=Identifier(f"generate-{pipeline_name}-file"),
                config=TaskConfig(
                    platform=Platform.linux,
                    image_resource=AnonymousResource(
                        type="registry-image",
                        source=_OL_INFRA_IMAGE_SOURCE,
                    ),
                    inputs=[
                        Input(name=Identifier("infrastructure-pipeline-definitions"))
                    ],
                    outputs=[Output(name=Identifier("pipeline"))],
                    params={"PYTHONPATH": "../infrastructure-pipeline-definitions/src"},
                    run=Command(
                        path="python",
                        dir="pipeline",
                        user="root",
                        args=[
                            f"../infrastructure-pipeline-definitions/{script_path}",
                        ],
                    ),
                ),
            ),
            SetPipelineStep(
                set_pipeline=Identifier(pipeline_name),
                file="pipeline/definition.json",
            ),
        ],
    )


def meta_pipeline() -> Pipeline:
    """Generate the meta pipeline managing all dedicated Pulumi infra pipelines."""
    pipeline_definitions = git_repo(
        name=Identifier("infrastructure-pipeline-definitions"),
        uri="https://github.com/mitodl/ol-infrastructure",
        branch="main",
        paths=[
            "src/ol_concourse/pipelines/infrastructure/",
            "pyproject.toml",
            "src/ol_concourse/pipelines/constants.py",
            "src/ol_concourse/pipelines/jobs.py",
        ],
    )

    jobs = [meta_job(name, path) for name, path in PIPELINE_CONFIGS]

    jobs.append(
        Job(
            name=Identifier("set-pulumi-infrastructure-meta-pipeline"),
            plan=[
                GetStep(
                    get="infrastructure-pipeline-definitions",
                    trigger=True,
                ),
                TaskStep(
                    task=Identifier(
                        "generate-pulumi-infrastructure-meta-pipeline-file"
                    ),
                    config=TaskConfig(
                        platform=Platform.linux,
                        image_resource=AnonymousResource(
                            type="registry-image",
                            source=_OL_INFRA_IMAGE_SOURCE,
                        ),
                        inputs=[
                            Input(
                                name=Identifier("infrastructure-pipeline-definitions")
                            )
                        ],
                        outputs=[Output(name=Identifier("pipeline"))],
                        params={
                            "PYTHONPATH": "../infrastructure-pipeline-definitions/src"
                        },
                        run=Command(
                            path="python",
                            dir="pipeline",
                            user="root",
                            args=[
                                "../infrastructure-pipeline-definitions/"
                                "src/ol_concourse/pipelines/infrastructure/meta.py",
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
        jobs=jobs,
    )


if __name__ == "__main__":
    pipeline_json = meta_pipeline().model_dump_json(indent=2)
    with open("definition.json", "w") as definition:  # noqa: PTH123
        definition.write(pipeline_json)
    sys.stdout.write(pipeline_json)
    print()  # noqa: T201
    print("fly -t pr-inf sp -p pulumi-infrastructure-meta -c definition.json")  # noqa: T201
