import sys

from ol_concourse.lib.containers import container_build_task, ensure_ecr_task
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
    PutStep,
    Resource,
    TaskConfig,
    TaskStep,
)
from ol_concourse.lib.resources import git_repo, registry_image

from ol_concourse.pipelines.constants import ECR_REGION, dockerhub_ecr_image_uri
from ol_concourse.pipelines.ecr import configure_ecr_repository_task
from ol_concourse.pipelines.versions_map import version_pin_paths

# The dagger version comes from bridge.lib.versions, not from a github_release
# resource tracking dagger/dagger. A tracked release with trigger=True republishes
# mitodl/dcind:latest the moment upstream cuts a tag, with nothing in between --
# the same auto-tracking that put Concourse 8.3.0 into production and broke every
# worker (see the note in pipelines/infrastructure/concourse/pipeline.py). Renovate
# bumps versions.py, the sync-version-pins hook regenerates the pin below inside
# that PR, and merging it is what rebuilds the image.
(DAGGER_VERSION_PIN,) = version_pin_paths("DAGGER_VERSION")

ol_inf_repo = git_repo(
    name=Identifier("ol-infrastructure-repository"),
    uri="https://github.com/mitodl/ol-infrastructure",
    branch="main",
    check_every="24h",
    paths=["dockerfiles/dcind/", DAGGER_VERSION_PIN],
)

COLLECT_DAGGER_VERSION = f"""set -e
version="$(cat {ol_inf_repo.name}/{DAGGER_VERSION_PIN})"
echo "DAGGER_VERSION=$version" > dagger-version/args_file
echo "$version" > dagger-version/tag_file
"""

dcind_release_image = Resource(
    name=Identifier("dcind-release-resource-image"),
    type="registry-image",
    icon="docker",
    source={
        "repository": "mitodl/dcind",
        "tag": "latest",
        "password": "((dockerhub.password))",
        "username": "((dockerhub.username))",
    },
)

dcind_ecr_image = registry_image(
    name=Identifier("dcind-release-resource-image-ecr"),
    image_repository="mitodl/dcind",
    image_tag="latest",
    ecr_region=ECR_REGION,
)


docker_pipeline = Pipeline(
    resources=[ol_inf_repo, dcind_release_image, dcind_ecr_image],
    jobs=[
        Job(
            name=Identifier("build-and-publish-container"),
            plan=[
                GetStep(get=ol_inf_repo.name, trigger=True),
                TaskStep(
                    task=Identifier("collect-dagger-version"),
                    config=TaskConfig(
                        platform=Platform.linux,
                        image_resource=AnonymousResource(
                            type="registry-image",
                            source={
                                "repository": dockerhub_ecr_image_uri("alpine"),
                                "tag": "3",
                                "aws_region": ECR_REGION,
                            },
                        ),
                        inputs=[Input(name=ol_inf_repo.name)],
                        outputs=[
                            Output(name=Identifier("dagger-version")),
                        ],
                        run=Command(
                            path="sh",
                            args=["-xc", COLLECT_DAGGER_VERSION],
                        ),
                    ),
                ),
                container_build_task(
                    inputs=[
                        Input(name=ol_inf_repo.name),
                        Input(name="dagger-version"),
                    ],
                    build_parameters={
                        "CONTEXT": f"{ol_inf_repo.name}/dockerfiles/dcind",
                        "BUILD_ARGS_FILE": "dagger-version/args_file",
                    },
                ),
                ensure_ecr_task("mitodl/dcind"),
                configure_ecr_repository_task("mitodl/dcind", keep_last_n_images=10),
                PutStep(
                    put=dcind_release_image.name,
                    inputs="detect",
                    params={
                        "image": "image/image.tar",
                        "additional_tags": ("./dagger-version/tag_file"),
                    },
                ),
                PutStep(
                    put=dcind_ecr_image.name,
                    inputs="detect",
                    params={
                        "image": "image/image.tar",
                        "additional_tags": ("./dagger-version/tag_file"),
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
        "\nfly -t pr-inf set-pipeline -p dcind-resource-image -c definition.json"
    )
