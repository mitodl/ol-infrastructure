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
# rebuild of the base — without them the hardening silently decays between
# Dockerfile edits.
#
# The build does NOT pull dhi.io directly. DHI's images carry zstd-compressed
# layers for anything Docker built and we didn't modify; buildkit's OCI
# exporter (which oci-build-task uses, with no documented option to
# override — confirmed by reading its source: neither the Config struct nor
# the buildctl invocation it shells out to expose a compression flag)
# preserves that original compression for pass-through layers rather than
# recompressing everything to match the new layers it adds.
#
# That alone is survivable — Concourse's registry-image resource genuinely
# can decode OCI zstd-labeled layers (verified by reading its source,
# commands/rootfs.go, at v1.18.0, the version bundled with our pinned
# Concourse 8.3.0: it has an explicit case for the OCI zstd media type using
# klauspost/compress/zstd). The actual break is one step further downstream.
# oci-build-task only requests OCI-format output (which can *label* a zstd
# layer correctly) when a build sets IMAGE_PLATFORM for multiple platforms;
# any single-platform build — i.e. nearly every k8s_apps app image, unlike
# this multi-arch ol-python-base build — defaults to Docker's legacy
# manifest format instead (verified: oci-build-task's task.go hardcodes
# `outputType := "docker"` unless multi-platform output forces OCI). That
# legacy format's manifest.json has no per-layer media-type field at all —
# there is no way to *declare* a layer zstd — so oci-build-task's loader for
# that path (tarball.ImageFromPath, go-containerregistry's legacy reader)
# pushes DHI's still-zstd-compressed bytes under the implicit legacy
# "tar.gzip" label, silently mislabeling them rather than transcoding them.
# Confirmed directly: `docker manifest inspect --verbose` on the exact
# learn-ai-app digest that failed the canary showed every layer declared as
# application/vnd.docker.image.rootfs.diff.tar.gzip — Docker's legacy gzip
# media type — despite carrying DHI's genuinely zstd-compressed bytes
# through unchanged. registry-image resource correctly trusts that label,
# tries gzip.NewReader on real zstd bytes, and fails with exactly the
# "gzip: invalid header" error that broke the canary build.
#
# mirror_dhi_python_base_task fixes this once, upstream of every consumer:
# it mirrors dhi.io into mitodl/dhi-python-mirror using skopeo's
# --dest-force-compress-format, which forces every layer — including ones
# copied verbatim from the source — to genuinely, not just nominally, gzip.
# Verified locally against the real dhi.io/python image: zstd layers came
# out uniformly gzip after the copy. With no zstd bytes left anywhere in the
# chain, there's nothing left for any downstream build's legacy-format
# output to mislabel, regardless of whether that build is single- or
# multi-platform.
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

DHI_MIRROR_REPOSITORY = "mitodl/dhi-python-mirror"


def mirror_dhi_python_base_task(python_version: str) -> TaskStep:
    """Mirror the DHI Python dev image with layers forced to gzip.

    skopeo takes registry credentials directly as flags, so unlike the
    docker-config dance below (needed because oci-build-task has no
    credential flags of its own), this task needs no separate config file.
    """
    tag = f"{python_version}-debian13-dev"
    return TaskStep(
        task=Identifier(f"mirror-dhi-python-{python_version.replace('.', '')}"),
        config=TaskConfig(
            platform=Platform.linux,
            image_resource=AnonymousResource(
                type=REGISTRY_IMAGE,
                source={"repository": "quay.io/skopeo/stable", "tag": "latest"},
            ),
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
                        "skopeo copy --all"
                        ' --src-creds="$DHI_USERNAME:$DHI_PASSWORD"'
                        ' --dest-creds="$DHI_USERNAME:$DHI_PASSWORD"'
                        " --dest-compress-format=gzip"
                        " --dest-force-compress-format"
                        f" docker://dhi.io/python:{tag}"
                        f" docker://{DHI_MIRROR_REPOSITORY}:{tag}"
                    ),
                ],
            ),
        ),
    )


def dockerhub_docker_config_task() -> TaskStep:
    """Write a docker config authorizing pulls from Docker Hub.

    Needed so the build's `FROM mitodl/dhi-python-mirror:...` pull is
    authenticated regardless of that repo's default visibility — cheaper
    than depending on a Docker Hub visibility setting staying put. oci-build-
    task resolves FROM-image credentials through the standard docker config
    machinery, honoring the DOCKER_CONFIG env var, so the build task consumes
    this task's output directory via DOCKER_CONFIG=docker-config.
    """
    docker_config = Output(name=Identifier("docker-config"))
    return TaskStep(
        task=Identifier("write-dockerhub-docker-config"),
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
                "DOCKERHUB_USERNAME": "((dockerhub.username))",
                "DOCKERHUB_PASSWORD": "((dockerhub.password))",
            },
            run=Command(
                path="sh",
                # -e only: -x would echo the credentials into the build log.
                args=[
                    "-ec",
                    (
                        'auth="$(printf \'%s:%s\' "$DOCKERHUB_USERNAME"'
                        ' "$DOCKERHUB_PASSWORD" | base64 | tr -d \'\\n\')"\n'
                        'printf \'{"auths": {"https://index.docker.io/v1/":'
                        ' {"auth": "%s"}}}\' "$auth"'
                        f" > {docker_config.name}/config.json\n"
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
            mirror_dhi_python_base_task(python_version),
            dockerhub_docker_config_task(),
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
                    # (image/image) instead of a tarball.
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
