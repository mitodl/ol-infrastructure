import sys

from ol_concourse.lib.containers import container_build_task
from ol_concourse.lib.models.pipeline import (
    GetStep,
    Identifier,
    Input,
    Job,
    Pipeline,
    PutStep,
)
from ol_concourse.lib.resources import git_repo, registry_image


def build_dagster_docker_pipeline() -> Pipeline:
    # data_platform_branch = "main"
    data_platform_branch = "md/issue_767"
    data_platform_repo = git_repo(
        Identifier("ol-data-platform"),
        uri="https://github.com/mitodl/ol-data-platform",
        branch=data_platform_branch,
    )

    mono_dagster_image = registry_image(
        name=Identifier("mono-dagster-image"),
        image_repository="mitodl/mono-dagster",
        username="((dockerhub.username))",
        password="((dockerhub.password))",  # noqa: S106
    )

    docker_build_job = Job(
        name="build-mono-dagster-image",
        plan=[
            GetStep(get=data_platform_repo.name, trigger=True),
            container_build_task(
                inputs=[Input(name=data_platform_repo.name)],
                build_parameters={
                    "CONTEXT": data_platform_repo.name,
                    "DOCKERFILE": f"{data_platform_repo.name}/dockerfiles/orchestrate/Dockerfile.global",  # noqa: E501
                },
                build_args=[],
            ),
            PutStep(
                put=mono_dagster_image.name,
                params={
                    "image": "image/image.tar",
                    "additional_tags": f"./{data_platform_repo.name}/.git/describe_ref",
                },
            ),
        ],
    )

    return Pipeline(
        resources=[data_platform_repo, mono_dagster_image],
        jobs=[docker_build_job],
    )


if __name__ == "__main__":
    with open("definition.json", "w") as definition:
        definition.write(build_dagster_docker_pipeline().json(indent=2))
    sys.stdout.write(build_dagster_docker_pipeline().json(indent=2))
    sys.stdout.writelines(
        ("\n", "fly -t pr-main sp -p docker-mono-dagster -c definition.json")
    )
