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
from ol_concourse.pipelines.ecr import configure_ecr_repository_task
from ol_concourse.pipelines.pipeline_output import pipeline_json_with_user_data

ocw_studio_repo = git_repo(
    name=Identifier("ocw-studio-repository"),
    uri="https://github.com/mitodl/ocw-studio",
    branch="master",
    check_every="24h",
    paths=[
        "docker/ocw-course-publisher/Dockerfile",
        "docker/ocw-course-publisher/tag",
    ],
)

ocw_course_publisher_image = Resource(
    name=Identifier("ocw-course-publisher-image"),
    type="registry-image",
    icon="docker",
    source={
        "repository": "mitodl/ocw-course-publisher",
        "tag": "latest",
        "username": "((dockerhub.username))",
        "password": "((dockerhub.password))",
    },
)

ocw_course_publisher_ecr_image = registry_image(
    name=Identifier("ocw-course-publisher-image-ecr"),
    image_repository="mitodl/ocw-course-publisher",
    image_tag="latest",
    ecr_region=ECR_REGION,
)

build_task = container_build_task(
    inputs=[Input(name=ocw_studio_repo.name)],
    build_parameters={
        "CONTEXT": f"{ocw_studio_repo.name}/docker/ocw-course-publisher",
        "DOCKERFILE": f"{ocw_studio_repo.name}/docker/ocw-course-publisher/Dockerfile",
    },
)

docker_pipeline = Pipeline(
    resources=[
        ocw_studio_repo,
        ocw_course_publisher_image,
        ocw_course_publisher_ecr_image,
    ],
    jobs=[
        Job(
            name=Identifier("build-and-publish-ocw-course-publisher-image"),
            plan=[
                GetStep(get=ocw_studio_repo.name, trigger=True),
                build_task,
                ensure_ecr_task("mitodl/ocw-course-publisher"),
                configure_ecr_repository_task(
                    "mitodl/ocw-course-publisher", keep_last_n_images=10
                ),
                PutStep(
                    put=ocw_course_publisher_image.name,
                    params={
                        "image": "image/image.tar",
                        "additional_tags": (
                            f"./{ocw_studio_repo.name}/docker/ocw-course-publisher/tag"
                        ),
                    },
                ),
                PutStep(
                    put=ocw_course_publisher_ecr_image.name,
                    params={
                        "image": "image/image.tar",
                        "additional_tags": (
                            f"./{ocw_studio_repo.name}/docker/ocw-course-publisher/tag"
                        ),
                    },
                ),
            ],
        )
    ],
)

if __name__ == "__main__":
    output = pipeline_json_with_user_data(
        docker_pipeline,
        user_data={
            "description": (
                "Builds `mitodl/ocw-course-publisher` from ocw-studio's "
                "`docker/ocw-course-publisher/` (Dockerfile plus a `tag` "
                "file). Publishes to Docker Hub and ECR, tagged from that "
                "repo's tag file."
            ),
            "team": "main",
            "category": "base-images",
        },
    )
    with open("definition.json", "w") as definition:  # noqa: PTH123
        definition.write(output)
    sys.stdout.write(output)
    sys.stdout.write(
        "\nfly -t pr-main set-pipeline -p ocw-course-publisher-image"
        " -c definition.json\n"
    )
