"""Read the storage format an omnigraph image declares, out of its ECR labels.

This stack deploys ``omnigraph-server`` by digest and has no access to
agent-kit at apply time, so until agent-kit#358 the format the binary inside
that image reads was unknowable here. ``validate_internal_schema_version``
could only compare two committed config values to each other, and said so:
"this does NOT verify either value against the image actually being deployed".
The consequence showed up on 2026-09-16 — a format-9 image deployed into a
cluster still serving fmt6, discovered by the cluster-apply Job failing three
times mid-deploy rather than by a preview a human reads first.

agent-kit's two Dockerfiles now stamp ``edu.mit.ol.omnigraph.internal-schema``
onto the runtime stage, mirrored from ``_OMNIGRAPH_INTERNAL_SCHEMA`` and
covered by ``just check-omnigraph-pins``. This module pulls that label back out
of ECR so ``validate_image_internal_schema`` has something to compare against.

Reading it takes two round trips because a registry does not serve labels
directly: ``batch_get_image`` returns the manifest, whose ``config.digest``
names the image config blob, and the labels live in that blob. Both are
metadata reads of a few kilobytes, not an image pull.

EVERY FAILURE HERE IS NON-FATAL BY DESIGN. An image built before the label
existed, an unreachable registry, a run without ECR read access — none of
those are reasons to refuse a deploy, and refusing on them would make rolling
back to a pre-label image impossible. They raise
``ImageSchemaUnavailableError``, the caller warns, and the check is skipped.

The one exception is a label that is PRESENT but not an integer, which is a
broken image build rather than an old image, and raises ``ValueError`` so the
preview fails. That is the shape an un-expanded build arg takes (the label
comes out ``""``), and reading it as "no label" would let a broken build switch
the gate off with nothing failing anywhere.
"""

import json
from typing import Any

import boto3
import requests
from botocore.exceptions import BotoCoreError, ClientError

#: The OCI label agent-kit's docker/omnigraph-server.Dockerfile and
#: docker/witan.Dockerfile stamp onto their runtime stage. Changing this string
#: on either side silently turns the check off, so it is asserted from the
#: agent-kit side by `just check-omnigraph-pins` and named in both Dockerfiles.
IMAGE_SCHEMA_LABEL = "edu.mit.ol.omnigraph.internal-schema"

#: Asked for explicitly because ECR otherwise returns whatever media type it
#: feels like, and a manifest we cannot parse is indistinguishable from an
#: image with no label. Both the Docker v2 and OCI spellings are listed: the
#: Concourse build pushes a single-platform Docker manifest today, and the
#: index types are here so a future multi-arch push degrades to the
#: linux/amd64 walk below rather than to an unreadable manifest.
_ACCEPTED_MEDIA_TYPES = [
    "application/vnd.docker.distribution.manifest.v2+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.oci.image.index.v1+json",
]

#: The config blob is a few KB from a presigned S3 URL. Short on purpose: this
#: runs inline in `pulumi preview`, and a hung registry should degrade to a
#: skipped check rather than to a preview that never returns.
_HTTP_TIMEOUT_SECONDS = 15


class ImageSchemaUnavailableError(Exception):
    """The deploying image's storage-format label could not be read.

    Carries the reason so the warning the caller emits says which of the
    non-fatal cases this was, rather than only that the check was skipped.
    """


def _image_id(image_ref: str) -> dict[str, str]:
    """Turn a fully-qualified image reference into an ECR ``imageId``.

    ``format_docker_image_ref`` produces exactly one of ``repo@sha256:...``
    (the pipeline's digest pin) or ``repo:tag`` (a local run with
    ``OMNIGRAPH_DOCKER_TAG``), so those are the only two shapes handled.

    :param image_ref: Fully-qualified reference, as deployed.
    :returns: ``{"imageDigest": ...}`` or ``{"imageTag": ...}``.
    :rtype: dict[str, str]
    """
    if "@" in image_ref:
        return {"imageDigest": image_ref.split("@", 1)[1]}
    _, separator, tag = image_ref.rpartition(":")
    if not separator or "/" in tag:
        msg = (
            f"image reference {image_ref!r} carries neither a digest nor a "
            "tag, so ECR cannot be asked about it."
        )
        raise ImageSchemaUnavailableError(msg)
    return {"imageTag": tag}


def _get_manifest(
    client,
    repository_name: str,
    image_id: dict[str, str],
) -> dict[str, Any]:
    """Fetch and parse one manifest from ECR.

    :param client: A boto3 ``ecr`` client.
    :param repository_name: ECR repository name, e.g. ``omnigraph-server``.
    :param image_id: An ECR ``imageId`` as built by :func:`_image_id`.
    :returns: The parsed manifest document.
    :rtype: dict[str, Any]
    """
    try:
        response = client.batch_get_image(
            repositoryName=repository_name,
            imageIds=[image_id],
            acceptedMediaTypes=_ACCEPTED_MEDIA_TYPES,
        )
    except (BotoCoreError, ClientError) as exc:
        msg = f"could not read {repository_name} {image_id} from ECR: {exc}"
        raise ImageSchemaUnavailableError(msg) from exc
    if not response["images"]:
        failures = response["failures"]
        msg = f"ECR returned no manifest for {repository_name} {image_id}: {failures}"
        raise ImageSchemaUnavailableError(msg)
    return json.loads(response["images"][0]["imageManifest"])


def read_image_internal_schema(
    repository_name: str, image_ref: str, region: str
) -> int:
    """Return the storage format the image at ``image_ref`` declares it reads.

    :param repository_name: ECR repository name, e.g. ``omnigraph-server``.
    :param image_ref: Fully-qualified reference, as deployed — the same string
        the Deployment's pod spec carries.
    :param region: AWS region the ECR repository lives in.
    :returns: The value of the ``edu.mit.ol.omnigraph.internal-schema`` label.
    :rtype: int
    :raises ImageSchemaUnavailableError: on an unreachable or unreadable registry,
        an unparseable manifest, or an image carrying no such label. All of
        these are non-fatal — see this module's docstring.
    """
    client = boto3.client("ecr", region_name=region)
    manifest = _get_manifest(client, repository_name, _image_id(image_ref))
    if "manifests" in manifest:
        # A manifest list/index. Pick the platform the cluster actually runs;
        # attestation entries in an index also appear here, with architecture
        # "unknown", which is why this matches rather than taking the first.
        for entry in manifest["manifests"]:
            platform = entry.get("platform", {})
            if (platform.get("os"), platform.get("architecture")) == ("linux", "amd64"):
                manifest = _get_manifest(
                    client, repository_name, {"imageDigest": entry["digest"]}
                )
                break
        else:
            msg = (
                f"{image_ref} is a multi-platform index with no linux/amd64 "
                "manifest, so there is no image config to read labels from."
            )
            raise ImageSchemaUnavailableError(msg)
    # The labels are in the image CONFIG blob, not the manifest. ECR serves a
    # blob by digest through a presigned URL, the same mechanism a `docker
    # pull` uses for layers.
    try:
        download_url = client.get_download_url_for_layer(
            repositoryName=repository_name,
            layerDigest=manifest["config"]["digest"],
        )["downloadUrl"]
        config = requests.get(download_url, timeout=_HTTP_TIMEOUT_SECONDS).json()
    except (BotoCoreError, ClientError, requests.RequestException, KeyError) as exc:
        msg = f"could not read the image config for {image_ref}: {exc}"
        raise ImageSchemaUnavailableError(msg) from exc
    # `Labels` is null, not absent, on an image that declares none.
    labels = config["config"].get("Labels") or {}
    declared = labels.get(IMAGE_SCHEMA_LABEL)
    if declared is None:
        msg = (
            f"{image_ref} carries no {IMAGE_SCHEMA_LABEL} label. Images built "
            "from agent-kit before #358 do not, so this is expected on a "
            "rollback and means only that the format check is skipped."
        )
        raise ImageSchemaUnavailableError(msg)
    try:
        return int(declared)
    except ValueError as exc:
        # NOT ImageSchemaUnavailableError, deliberately: absent means an older
        # image and is skipped, but present-and-unparseable means the build
        # that produced this image is broken. The empty string is the specific
        # way that happens — a Dockerfile stage that fails to re-declare the
        # ARG expands the label to "" and still builds clean. Treating that as
        # "no label" would let a broken build turn the gate off silently, so it
        # fails the preview instead.
        msg = (
            f"{image_ref} declares {IMAGE_SCHEMA_LABEL}={declared!r}, which is "
            "not an integer storage-format number. An empty value means the "
            "image build did not expand OMNIGRAPH_INTERNAL_SCHEMA — see "
            "agent-kit's `just check-omnigraph-pins`. Rebuild the image; do "
            "not work around this by deploying it anyway."
        )
        raise ValueError(msg) from exc
