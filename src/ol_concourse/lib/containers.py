from ol_concourse.lib.jobs.infrastructure import Output
from ol_concourse.lib.models.pipeline import (
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
