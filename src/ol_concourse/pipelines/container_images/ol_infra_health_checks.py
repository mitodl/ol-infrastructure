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

ol_infra_health_checks_repository = git_repo(
    name=Identifier("ol-infra-health-checks-github"),
    uri="https://github.com/mitodl/ol-infra-health-checks",
    branch="main",
)

ol_infra_health_checks_image = Resource(
    name=Identifier("ol-infra-health-checks-image"),
    type="registry-image",
    icon="docker",
    source={
        "repository": "mitodl/ol-infra-health-checks",
        "tag": "latest",
        "password": "((dockerhub.password))",
        "username": "((dockerhub.username))",
    },
)

ol_infra_health_checks_ecr_image = registry_image(
    name=Identifier("ol-infra-health-checks-image-ecr"),
    image_repository="mitodl/ol-infra-health-checks",
    image_tag="latest",
    ecr_region=ECR_REGION,
)

build_task = container_build_task(
    inputs=[Input(name=ol_infra_health_checks_repository.name)],
    build_parameters={"CONTEXT": ol_infra_health_checks_repository.name},
)

docker_pipeline = Pipeline(
    resources=[
        ol_infra_health_checks_repository,
        ol_infra_health_checks_image,
        ol_infra_health_checks_ecr_image,
    ],
    jobs=[
        Job(
            name=Identifier("build-and-publish-container"),
            plan=[
                GetStep(get=ol_infra_health_checks_repository.name, trigger=True),
                build_task,
                ensure_ecr_task("mitodl/ol-infra-health-checks"),
                PutStep(
                    put=ol_infra_health_checks_image.name,
                    params={
                        "image": "image/image.tar",
                        "additional_tags": f"./{ol_infra_health_checks_repository.name}/.git/describe_ref",  # noqa: E501
                    },
                ),
                PutStep(
                    put=ol_infra_health_checks_ecr_image.name,
                    params={
                        "image": "image/image.tar",
                        "additional_tags": f"./{ol_infra_health_checks_repository.name}/.git/describe_ref",  # noqa: E501
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
        "fly -t <prod_target> set-pipeline -p docker-ol-infra-health-checks -c definition.json"  # noqa: E501
    )
