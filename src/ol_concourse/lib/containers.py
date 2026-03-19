from ol_concourse.lib.jobs.infrastructure import Output
from ol_concourse.lib.models.pipeline import (
    AnonymousResource,
    Cache,
    Command,
    Identifier,
    Input,
    TaskConfig,
    TaskStep,
)


def container_build_task(
    inputs: list[Input],
    build_parameters: dict[str, str] | None,
    build_args: list[str] | None = None,
) -> TaskStep:
    return TaskStep(
        task=Identifier("build-container-image"),
        privileged=True,
        config=TaskConfig(
            platform="linux",
            image_resource={
                "type": "registry-image",
                "source": {"repository": "concourse/oci-build-task"},
            },
            params=build_parameters,
            caches=[Cache(path="cache")],
            run=Command(
                path="build",
                args=build_args or [],
            ),
            inputs=inputs,
            # This output needs to be named exactly "image" or it won't actually export
            # the built image.
            outputs=[Output(name=Identifier("image"))],
        ),
    )


def ensure_ecr_task(ecr_repo_name: str) -> TaskStep:
    """Return a TaskStep that creates an ECR repository if it does not exist.

    Uses the AWS CLI with instance credentials (IRSA / worker IAM role).
    Safe to run on every pipeline execution: ``describe-repositories`` is a
    no-op when the repo already exists, and ``create-repository`` only runs
    when it does not.

    Args:
        ecr_repo_name: The ECR repository name *without* the registry host,
            e.g. ``"mitodl/graders-mit-600x"``.
    """
    return TaskStep(
        task=Identifier("ensure-ecr-repository"),
        config=TaskConfig(
            platform="linux",
            image_resource=AnonymousResource(
                type="registry-image",
                source={"repository": "amazon/aws-cli", "tag": "latest"},
            ),
            params={
                "REPO_NAME": ecr_repo_name,
                "AWS_PAGER": "cat",
            },
            run=Command(
                path="sh",
                args=[
                    "-exc",
                    (
                        "aws ecr describe-repositories"
                        " --repository-names ${REPO_NAME}"
                        " || aws ecr create-repository"
                        " --repository-name ${REPO_NAME}"
                    ),
                ],
            ),
        ),
    )
