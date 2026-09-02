import sys

from ol_concourse.lib.constants import REGISTRY_IMAGE
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
    TaskConfig,
    TaskStep,
)
from ol_concourse.lib.resources import git_repo, registry_image

from ol_concourse.pipelines.constants import ECR_REGION, dockerhub_ecr_image_uri
from ol_concourse.pipelines.ecr import configure_ecr_repository_task

PYTHON_VERSIONS = ("3.11", "3.12", "3.13", "3.14")

ol_infrastructure_repo = git_repo(
    name=Identifier("ol-infrastructure"),
    uri="https://github.com/mitodl/ol-infrastructure",
    branch="main",
    paths=["dockerfiles/ol-python-base/Dockerfile"],
)

# Upstream Docker Hardened Images bases (see the hardened-images RFC). These
# resources exist to trigger rebuilds whenever Docker ships a CVE-patched
# rebuild of the base -- without them the hardening silently decays between
# Dockerfile edits. dhi.io denies anonymous pulls, so the build authenticates
# via dhi_docker_config_task below.
#
# This build itself never mislabels DHI's zstd layers: it's multi-arch
# (IMAGE_PLATFORM below), and oci-build-task treats any multi-platform build
# as requiring OCI output regardless of any other setting, which can
# correctly declare a zstd layer. The mislabeling only happens in
# *downstream* single-platform builds (every app image) that don't request
# OCI output explicitly -- see OUTPUT_OCI in
# src/ol_concourse/pipelines/infrastructure/k8s_apps/pipeline.py and
# mitodl/ol-infrastructure#5714 for the full mechanism and why an earlier
# attempt at this (mirroring the base with forced recompression, #5712)
# was reverted (#5715) instead of fixed forward.
dhi_python_base_resources = {
    version: registry_image(
        name=Identifier(f"dhi-python-{version.replace('.', '')}-image"),
        image_repository="dhi.io/python",
        image_tag=f"{version}-debian13-dev",
        username="((dockerhub.username))",
        password="((dockerhub.password))",  # noqa: S106
    )
    for version in PYTHON_VERSIONS
}


def dhi_docker_config_task() -> TaskStep:
    """Write a docker config authorizing pulls from dhi.io.

    oci-build-task resolves FROM-image credentials through the standard
    docker config machinery, honoring the DOCKER_CONFIG env var, so the
    build task consumes this task's output directory via
    DOCKER_CONFIG=docker-config.
    """
    docker_config = Output(name=Identifier("docker-config"))
    return TaskStep(
        task=Identifier("write-dhi-docker-config"),
        config=TaskConfig(
            platform=Platform.linux,
            image_resource=AnonymousResource(
                type=REGISTRY_IMAGE,
                source={
                    "repository": dockerhub_ecr_image_uri("alpine"),
                    "tag": "latest",
                    "aws_region": ECR_REGION,
                },
            ),
            outputs=[docker_config],
            params={
                "DHI_USERNAME": "((dockerhub.username))",
                "DHI_PASSWORD": "((dockerhub.password))",
            },
            run=Command(
                path="sh",
                # -e only: -x would echo the credentials into the build log.
                args=[
                    "-ec",
                    (
                        'auth="$(printf \'%s:%s\' "$DHI_USERNAME"'
                        ' "$DHI_PASSWORD" | base64 | tr -d \'\\n\')"\n'
                        'printf \'{"auths": {"dhi.io": {"auth": "%s"}}}\''
                        f' "$auth" > {docker_config.name}/config.json\n'
                    ),
                ],
            ),
        ),
    )


image_resources = {
    version: registry_image(
        name=Identifier(f"ol-python-base-{version.replace('.', '')}-image"),
        image_repository="mitodl/ol-python-base",
        image_tag=version,
        username="((dockerhub.username))",
        password="((dockerhub.password))",  # noqa: S106
    )
    for version in PYTHON_VERSIONS
}

ecr_image_resources = {
    version: registry_image(
        name=Identifier(f"ol-python-base-{version.replace('.', '')}-image-ecr"),
        image_repository="mitodl/ol-python-base",
        image_tag=version,
        ecr_region=ECR_REGION,
    )
    for version in PYTHON_VERSIONS
}


def build_job(python_version: str) -> Job:
    context = f"{ol_infrastructure_repo.name}/dockerfiles/ol-python-base"
    return Job(
        name=Identifier(f"build-and-publish-{python_version}"),
        public=True,
        plan=[
            GetStep(get=ol_infrastructure_repo.name, trigger=True),
            GetStep(
                get=dhi_python_base_resources[python_version].name,
                trigger=True,
            ),
            dhi_docker_config_task(),
            container_build_task(
                inputs=[
                    Input(name=ol_infrastructure_repo.name),
                    Input(name=Identifier("docker-config")),
                ],
                build_parameters={
                    "CONTEXT": context,
                    "BUILD_ARG_PYTHON_VERSION": python_version,
                    # Auth for the dhi.io FROM pull; written by
                    # dhi_docker_config_task above.
                    "DOCKER_CONFIG": "docker-config",
                    # Cross-build linux/arm64 via QEMU emulation (workers have
                    # qemu-user-static/binfmt-support installed) so Apple
                    # Silicon can pull a native image. Multiple platforms
                    # makes oci-build-task emit an OCI layout directory
                    # (image/image) instead of a tarball, and treats the
                    # build as requiring OCI output regardless of any other
                    # setting -- so this build never mislabels DHI's zstd
                    # layers even without an explicit OUTPUT_OCI.
                    "IMAGE_PLATFORM": "linux/amd64,linux/arm64",
                },
            ),
            ensure_ecr_task("mitodl/ol-python-base"),
            configure_ecr_repository_task("mitodl/ol-python-base"),
            PutStep(
                put=image_resources[python_version].name,
                inputs="detect",
                params={"image": "image/image"},
            ),
            PutStep(
                put=ecr_image_resources[python_version].name,
                inputs="detect",
                params={"image": "image/image"},
            ),
        ],
    )


ol_python_base_pipeline = Pipeline(
    resources=[
        ol_infrastructure_repo,
        *dhi_python_base_resources.values(),
        *image_resources.values(),
        *ecr_image_resources.values(),
    ],
    jobs=[build_job(v) for v in PYTHON_VERSIONS],
)

if __name__ == "__main__":
    with open("definition.json", "w") as definition:  # noqa: PTH123
        definition.write(ol_python_base_pipeline.model_dump_json(indent=2))
    sys.stdout.write(ol_python_base_pipeline.model_dump_json(indent=2))
    sys.stdout.write(
        "\nfly -t pr-inf set-pipeline -p ol-python-base-docker -c definition.json\n"
    )
