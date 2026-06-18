import sys

from ol_concourse.lib.containers import container_build_task, ensure_ecr_task
from ol_concourse.lib.models.pipeline import (
    GetStep,
    Identifier,
    Input,
    Job,
    Pipeline,
    PutStep,
    Resource,
)
from ol_concourse.lib.resources import git_repo, registry_image

from ol_concourse.pipelines.constants import ECR_REGION

concourse_github_issues_repository = git_repo(
    name=Identifier("ol-concourse-github-issues"),
    uri="https://github.com/mitodl/ol-concourse-github-issues",
    branch="main",
    check_every="24h",
)

concourse_github_issues_image = Resource(
    name=Identifier("ol-concourse-github-issues-image"),
    type="registry-image",
    icon="docker",
    source={
        "repository": "mitodl/ol-concourse-github-issues",
        "tag": "latest",
        "password": "((dockerhub.password))",
        "username": "((dockerhub.username))",
    },
)

concourse_github_issues_ecr_image = registry_image(
    name=Identifier("ol-concourse-github-issues-image-ecr"),
    image_repository="mitodl/ol-concourse-github-issues",
    image_tag="latest",
    ecr_region=ECR_REGION,
)

build_task = container_build_task(
    inputs=[Input(name=concourse_github_issues_repository.name)],
    build_parameters={"CONTEXT": concourse_github_issues_repository.name},
)

docker_pipeline = Pipeline(
    resources=[
        concourse_github_issues_repository,
        concourse_github_issues_image,
        concourse_github_issues_ecr_image,
    ],
    jobs=[
        Job(
            name=Identifier("build-and-publish-container"),
            plan=[
                GetStep(get=concourse_github_issues_repository.name, trigger=True),
                build_task,
                ensure_ecr_task("mitodl/ol-concourse-github-issues"),
                PutStep(
                    put=concourse_github_issues_image.name,
                    params={
                        "image": "image/image.tar",
                        "additional_tags": f"./{concourse_github_issues_repository.name}/.git/describe_ref",  # noqa: E501
                    },
                ),
                PutStep(
                    put=concourse_github_issues_ecr_image.name,
                    params={
                        "image": "image/image.tar",
                        "additional_tags": f"./{concourse_github_issues_repository.name}/.git/describe_ref",  # noqa: E501
                    },
                ),
            ],
        )
    ],
)

if __name__ == "__main__":
    with open("definition.json", "w") as definition:  # noqa: PTH123
        definition.write(docker_pipeline.model_dump_json(indent=2))
    sys.stdout.write(docker_pipeline.model_dump_json(indent=2))
    sys.stdout.write(
        "fly -t <prod_target> set-pipeline -p docker-ol-concourse-github-issues-image -c definition.json"  # noqa: E501
    )
