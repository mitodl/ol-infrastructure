from ol_concourse.lib.constants import REGISTRY_IMAGE
from ol_concourse.lib.models.pipeline import (
    AnonymousResource,
    Command,
    Identifier,
    Job,
    Pipeline,
    Platform,
    RegistryImage,
    TaskConfig,
    TaskStep,
)


def hello_pipeline() -> Pipeline:
    hello_job_object = Job(
        name=Identifier("deploy-hello-world"),
        max_in_flight=1,  # Only allow 1 Pulumi task at a time since they lock anyway.
        plan=[
            TaskStep(
                task=Identifier("hello-task"),
                config=TaskConfig(
                    platform=Platform.linux,
                    image_resource=AnonymousResource(
                        type=REGISTRY_IMAGE,
                        source=RegistryImage(repository="busybox"),
                    ),
                    run=Command(path="echo", args=["Hello, World!"]),
                ),
            ),
        ],
    )
    return Pipeline(jobs=[hello_job_object])


if __name__ == "__main__":
    import sys

    with open("definition.json", "w") as definition:  # noqa: PTH123
        definition.write(hello_pipeline().model_dump_json(indent=2))
    sys.stdout.write(hello_pipeline().model_dump_json(indent=2))
    print()  # noqa: T201
    print("fly -t pr-inf sp -p misc-cloud-hello -c definition.json")  # noqa: T201
