from concourse.lib.constants import REGISTRY_IMAGE
from concourse.lib.models.pipeline import (
    AnonymousResource,
    Command,
    GetStep,
    Identifier,
    Input,
    Job,
    Output,
    Pipeline,
    Platform,
    PutStep,
    RegistryImage,
    Resource,
    TaskConfig,
    TaskStep,
)
from concourse.lib.resources import git_repo

ovs_release = git_repo(
    Identifier("odl-video-service"),
    uri="https://github.com/mitodl/odl-video-service",
    branch="master",
    check_every="60m",
)

ovs_image_resource = AnonymousResource(
    type=REGISTRY_IMAGE, source=RegistryImage(repository="concourse/oci-build-task")
)

docker_registry_image = Resource(
    name=Identifier("ovs-image"),
    type="registry-image",
    icon="docker",
    source={
        "password": "((dockerhub.password))",
        "username": "((dockerhub.username))",
        "repository": "mitodl/ovs-app",
        "tag": "latest",
    },
)


def docker_pipeline() -> Pipeline:
    build_and_push_job = Job(
        name="build-and-push",
        build_log_retention={"builds": 10},
        plan=[
            GetStep(get=ovs_release.name, trigger=True),
            TaskStep(
                task=Identifier("build-ovs-image"),
                privileged=True,
                config=TaskConfig(
                    platform=Platform.linux,
                    image_resource=ovs_image_resource,
                    inputs=[Input(name=ovs_release.name)],
                    outputs=[Output(name="image")],
                    params={"CONTEXT": "odl-video-service", "TARGET": "production"},
                    run=Command(
                        path="build",
                        args=[],
                    ),
                ),
            ),
            PutStep(
                put=docker_registry_image.name,
                params={
                    "image": "image/image.tar",
                    "additional_tags": f"./{ovs_release.name}/.git/describe_ref",
                },
            ),
        ],
    )
    return Pipeline(
        resources=[ovs_release, docker_registry_image], jobs=[build_and_push_job]
    )


if __name__ == "__main__":
    import sys

    with open("definition.json", "w") as definition:
        definition.write(docker_pipeline().json(indent=2))
    sys.stdout.write(docker_pipeline().json(indent=2))
    print()
    print("fly -t pr-inf sp -p docker-ovs-image -c definition.json")
