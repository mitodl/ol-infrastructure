from pathlib import Path

from ol_concourse.lib.models.pipeline import Duration

GH_ISSUES_DEFAULT_REPOSITORY = "ol-platform-eng/concourse-workflow"

# The github-issues resource defaults to polling the github.mit.edu enterprise
# appliance (gh_host defaults to https://github.mit.edu/api/v3). That appliance
# is under heavy load, so pipelines gating on it should poll less often than
# the resource's own 60m default.
GH_ISSUES_ENTERPRISE_POLL_FREQUENCY = Duration("4h")

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
        ``"mitodl/ol-infrastructure"``.  Official library images (no namespace) are
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
# Paths every Pulumi pipeline watches, regardless of which project it deploys.
#
# NOTE: this deliberately does NOT include "src/bridge/secrets/".  Watching the
# whole secrets tree meant a change to any one application's secret file
# re-triggered every Pulumi pipeline in both Concourse instances.  Each pipeline
# now watches only the secrets its own project decrypts, via
# `ol_concourse.pipelines.secrets_map.project_secrets_paths`.  What remains
# below is the SOPS decryption machinery itself -- the helper module, and the
# vendored sops binaries it shells out to -- which every project does run.
PULUMI_WATCHED_PATHS = [
    "src/ol_infrastructure/lib/",
    "src/ol_infrastructure/components/",
    "pipelines/infrastructure/scripts/",
    "src/bridge/secrets/sops.py",
    "src/bridge/secrets/__init__.py",
    "src/bridge/secrets/bin/",
]
