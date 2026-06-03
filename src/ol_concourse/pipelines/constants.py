from pathlib import Path

GH_ISSUES_DEFAULT_REPOSITORY = "ol-platform-eng/concourse-workflow"

# ECR pull-through cache for Docker Hub — avoids Docker Hub rate limits.
# Set aws_region (inline dicts) or ecr_region (registry_image()) alongside
# image repos returned by dockerhub_ecr_image_uri().
ECR_REGION = "us-east-1"


def dockerhub_ecr_image_uri(image_repo: str) -> str:
    """Return the ECR pull-through cache repository path for a Docker Hub image.

    Routes image pulls through the ECR ``dockerhub`` pull-through cache prefix to
    avoid Docker Hub anonymous/authenticated rate limits in Concourse pipelines.

    Use this as:
    - ``image_repository`` in :func:`~ol_concourse.lib.resources.registry_image`
      together with ``ecr_region=ECR_REGION``.
    - ``"repository"`` in an inline ``image_resource`` dict together with
      ``"aws_region": ECR_REGION``.

    :param image_repo: Docker Hub image name, e.g. ``"alpine"`` or
        ``"grafana/grizzly"``.  Official library images (no namespace) are
        automatically prefixed with ``library/``.
    :returns: ECR pull-through repository path, e.g. ``"dockerhub/library/alpine"``.
    """
    if "/" not in image_repo:
        image_repo = f"library/{image_repo}"
    return f"dockerhub/{image_repo}"


PACKER_WATCHED_PATHS = [
    "src/bilder/images/packer.pkr.hcl",
    "src/bilder/images/config.pkr.hcl",
    "src/bilder/images/variables.pkr.hcl",
    "src/bilder/components/",
]
PULUMI_CODE_PATH = Path("src/ol_infrastructure")
PULUMI_WATCHED_PATHS = [
    "src/ol_infrastructure/lib/",
    "src/ol_infrastructure/components/",
    "pipelines/infrastructure/scripts/",
    "src/bridge/secrets/",
]
