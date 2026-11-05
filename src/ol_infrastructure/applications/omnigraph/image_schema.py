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

The same images also label the omnigraph HTTP contracts they speak, which the
deploy-order check (``validate_http_api_contract``) reads with the same rules.
"""

import json
import re
from collections.abc import Sequence
from typing import Any

import boto3
import requests
from botocore.config import Config as BotoConfig
from botocore.exceptions import BotoCoreError, ClientError

from ol_infrastructure.applications.omnigraph.scripts.check_council_health import (
    SUPPORTED_HTTP_API_CONTRACTS as PROBE_HTTP_API_CONTRACTS,
)

#: The OCI label agent-kit's docker/omnigraph-server.Dockerfile and
#: docker/witan.Dockerfile stamp onto their runtime stage. Changing this string
#: on either side silently turns the check off, so it is asserted from the
#: agent-kit side by `just check-omnigraph-pins` and named in both Dockerfiles.
IMAGE_SCHEMA_LABEL = "edu.mit.ol.omnigraph.internal-schema"

#: The oldest format the image's binary opens in place (agent-kit's
#: ``_OMNIGRAPH_MIN_INTERNAL_SCHEMA``). omnigraph 0.14 writes 16 and opens 14,
#: so a 0.14 image serves a fmt14 root without a rebuild. Images built before
#: the label serve only the format they write.
MIN_IMAGE_SCHEMA_LABEL = "edu.mit.ol.omnigraph.min-internal-schema"

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
#: skipped check rather than to a preview that crawls.
_HTTP_TIMEOUT_SECONDS = 15

#: Applied to the ECR calls for the same reason. botocore's defaults are 60s
#: connect and 60s read with retries, so a blackholed endpoint would stall a
#: preview for minutes before the check gave up and skipped — bounded, but not
#: what the timeout above is there to promise. These are metadata reads of a
#: few kilobytes against a regional endpoint.
_ECR_CLIENT_CONFIG = BotoConfig(
    connect_timeout=5, read_timeout=10, retries={"max_attempts": 2}
)


class ImageSchemaUnavailableError(Exception):
    """The deploying image's storage-format label could not be read.

    Carries the reason so the warning the caller emits says which of the
    non-fatal cases this was, rather than only that the check was skipped.
    """


class ImageLabelMissingError(ImageSchemaUnavailableError):
    """The image was read and carries no such label: it predates the label.

    Split from a registry that couldn't be read, because a caller reading an
    image it is deploying right now knows the image exists, so only this case
    is a legitimate rollback rather than a fault to fail on.
    """


def _mapping(value: object) -> dict[str, Any]:
    """Return ``value`` when it is a JSON object, else an empty mapping.

    A registry document is untrusted input, and JSON has no schema: a key can
    be present and ``null``, or hold a string where an object is expected.
    Every traversal below goes through this, because ``.get`` on a ``None``
    raises ``AttributeError`` and that would abort a preview rather than skip
    the check — the one thing this module promises not to do.

    :param value: A value pulled out of a parsed registry document.
    :returns: The mapping, or ``{}``.
    :rtype: dict[str, Any]
    """
    return value if isinstance(value, dict) else {}


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
    # Inside a try because non-fatality here is absolute: a registry serving a
    # manifest this cannot parse must skip the check, not abort an unrelated
    # preview with a JSONDecodeError from three frames down.
    try:
        manifest = json.loads(response["images"][0]["imageManifest"])
    except (ValueError, KeyError, IndexError) as exc:
        msg = f"ECR returned an unparseable manifest for {image_id}: {exc}"
        raise ImageSchemaUnavailableError(msg) from exc
    if not isinstance(manifest, dict):
        msg = f"ECR returned a non-object manifest for {image_id}: {type(manifest)}"
        raise ImageSchemaUnavailableError(msg)
    return manifest


def _image_labels(repository_name: str, image_ref: str, region: str) -> dict[str, Any]:
    """Return the OCI labels of the linux/amd64 image at ``image_ref``.

    :param repository_name: ECR repository name, e.g. ``omnigraph-server``.
    :param image_ref: Fully-qualified reference, as deployed.
    :param region: AWS region the ECR repository lives in.
    :returns: The image config's labels, ``{}`` when it declares none.
    :rtype: dict[str, Any]
    :raises ImageSchemaUnavailableError: on an unreachable or unreadable
        registry or an unparseable manifest.
    """
    client = boto3.client("ecr", region_name=region, config=_ECR_CLIENT_CONFIG)
    manifest = _get_manifest(client, repository_name, _image_id(image_ref))
    if "manifests" in manifest:
        # A manifest list/index. Pick the platform the cluster actually runs;
        # attestation entries in an index also appear here, with architecture
        # "unknown", which is why this matches rather than taking the first.
        entries = manifest["manifests"]
        if not isinstance(entries, list):
            msg = (
                f"{image_ref}'s index has a non-list `manifests`, so there is "
                "nothing to walk."
            )
            raise ImageSchemaUnavailableError(msg)
        for raw_entry in entries:
            entry = _mapping(raw_entry)
            platform = _mapping(entry.get("platform"))
            if (platform.get("os"), platform.get("architecture")) == ("linux", "amd64"):
                child = entry.get("digest")
                if not child:
                    msg = (
                        f"{image_ref}'s linux/amd64 index entry carries no "
                        "digest, so there is no manifest to follow."
                    )
                    raise ImageSchemaUnavailableError(msg)
                manifest = _get_manifest(
                    client, repository_name, {"imageDigest": child}
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
            layerDigest=_mapping(manifest.get("config"))["digest"],
        )["downloadUrl"]
        config = requests.get(download_url, timeout=_HTTP_TIMEOUT_SECONDS).json()
    except (
        BotoCoreError,
        ClientError,
        requests.RequestException,
        ValueError,
        KeyError,
    ) as exc:
        msg = f"could not read the image config for {image_ref}: {exc}"
        raise ImageSchemaUnavailableError(msg) from exc
    return _labels_of(config, image_ref)


def _labels_of(config: object, image_ref: str) -> dict[str, Any]:
    """Return the labels in a parsed image-config blob.

    `config` is optional in the OCI image-config spec and `Labels` is null
    rather than absent on an image that declares none, so both of those read
    as "no labels". Any other shape is a broken document, not an image
    without labels: it raises the plain (non-missing-label) error, so a caller
    that treats a missing label as legitimate doesn't treat this so.

    :param config: The parsed image-config blob.
    :param image_ref: The image, for the message.
    :returns: The labels, ``{}`` when the image declares none.
    :rtype: dict[str, Any]
    :raises ImageSchemaUnavailableError: on any other shape.
    """
    if not isinstance(config, dict):
        msg = f"the image config for {image_ref} is not a JSON object"
        raise ImageSchemaUnavailableError(msg)
    inner = config.get("config")
    if inner is None:
        return {}
    labels = inner.get("Labels") if isinstance(inner, dict) else inner
    if labels is None:
        return {}
    if not isinstance(inner, dict) or not isinstance(labels, dict):
        msg = f"the image config for {image_ref} has a malformed config.Labels"
        raise ImageSchemaUnavailableError(msg)
    return labels


def read_image_internal_schema_range(
    repository_name: str, image_ref: str, region: str
) -> tuple[int, int]:
    """Return the storage formats the image at ``image_ref`` declares it reads.

    :param repository_name: ECR repository name, e.g. ``omnigraph-server``.
    :param image_ref: Fully-qualified reference, as deployed — the same string
        the Deployment's pod spec carries.
    :param region: AWS region the ECR repository lives in.
    :returns: ``(oldest served, written)``: the
        ``edu.mit.ol.omnigraph.min-internal-schema`` and
        ``edu.mit.ol.omnigraph.internal-schema`` labels. An image without the
        first serves only the format it writes.
    :rtype: tuple[int, int]
    :raises ImageSchemaUnavailableError: on an unreachable or unreadable registry,
        an unparseable manifest, or an image carrying no such label. All of
        these are non-fatal — see this module's docstring.
    :raises ValueError: when either label is present but not an integer.
    """
    labels = _image_labels(repository_name, image_ref, region)
    if labels.get(IMAGE_SCHEMA_LABEL) is None:
        msg = (
            f"{image_ref} carries no {IMAGE_SCHEMA_LABEL} label. Images built "
            "from agent-kit before #358 do not, so this is expected on a "
            "rollback and means only that the format check is skipped."
        )
        raise ImageSchemaUnavailableError(msg)
    written = _int_label(
        labels, IMAGE_SCHEMA_LABEL, "OMNIGRAPH_INTERNAL_SCHEMA", image_ref
    )
    if labels.get(MIN_IMAGE_SCHEMA_LABEL) is None:
        return written, written
    oldest = _int_label(
        labels, MIN_IMAGE_SCHEMA_LABEL, "OMNIGRAPH_MIN_INTERNAL_SCHEMA", image_ref
    )
    return oldest, written


def _int_label(
    labels: dict[str, Any], label: str, build_arg: str, image_ref: str
) -> int:
    """Parse one storage-format label that is known to be present.

    :param labels: The image config's labels.
    :param label: The label to parse.
    :param build_arg: The agent-kit build arg that fills it, for the message.
    :param image_ref: The image, for the message.
    :returns: The label as an integer.
    :rtype: int
    :raises ValueError: when the value is not an integer.
    """
    declared = labels[label]
    try:
        return int(declared)
    except (TypeError, ValueError) as exc:
        # NOT ImageSchemaUnavailableError, deliberately: absent means an older
        # image and is skipped, but present-and-unparseable means the build
        # that produced this image is broken. The empty string is the specific
        # way that happens — a Dockerfile stage that fails to re-declare the
        # ARG expands the label to "" and still builds clean. Treating that as
        # "no label" would let a broken build turn the gate off silently, so it
        # fails the preview instead.
        msg = (
            f"{image_ref} declares {label}={declared!r}, which is "
            "not an integer storage-format number. An empty value means the "
            f"image build did not expand {build_arg} — see "
            "agent-kit's `just check-omnigraph-pins`. Rebuild the image; do "
            "not work around this by deploying it anyway."
        )
        raise ValueError(msg) from exc


#: The HTTP contract an omnigraph-server image speaks (agent-kit
#: ``OMNIGRAPH_HTTP_API_CONTRACT``, e.g. ``0.14``).
HTTP_API_CONTRACT_LABEL = "edu.mit.ol.omnigraph.http-api-contract"

#: Every HTTP contract a witan image's client negotiates, comma-separated
#: (agent-kit ``WITAN_HTTP_API_CONTRACTS``, e.g. ``0.14,0.13``). Both names are
#: held to witan-core's declarations by agent-kit's `just check-omnigraph-pins`.
HTTP_API_CONTRACTS_LABEL = "edu.mit.ol.omnigraph.http-api-contracts"

_CONTRACT = re.compile(r"\d+\.\d+")


def repository_name(image_ref: str) -> str:
    """Return the ECR repository name in a fully-qualified image reference.

    :param image_ref: ``<registry>/<name>@sha256:...`` or ``<registry>/<name>:<tag>``.
    :returns: ``<name>``, e.g. ``witan``.
    :rtype: str
    """
    path = image_ref.split("/", 1)[-1]
    if "@" in path:
        return path.split("@", 1)[0]
    return path.rsplit(":", 1)[0]


def read_image_http_api_contracts(
    image_ref: str, region: str, label: str
) -> tuple[str, ...]:
    """Return the omnigraph HTTP contracts the image at ``image_ref`` labels.

    :param image_ref: Fully-qualified reference, as deployed.
    :param region: AWS region the ECR repository lives in.
    :param label: ``HTTP_API_CONTRACT_LABEL`` for a server image,
        ``HTTP_API_CONTRACTS_LABEL`` for a witan image.
    :returns: The contracts, in label order.
    :rtype: tuple[str, ...]
    :raises ImageLabelMissingError: when the image was built before the label
        existed.
    :raises ImageSchemaUnavailableError: on an unreadable registry. Both are
        non-fatal to the omnigraph check, as for the format label.
    :raises ValueError: when the label is present but isn't a comma-separated
        list of ``<major>.<minor>`` contracts, which means a broken build.
    """
    labels = _image_labels(repository_name(image_ref), image_ref, region)
    # Only an absent key is a pre-label image. A key present with a null or
    # empty value is a broken build and falls through to the ValueError below.
    if label not in labels:
        msg = (
            f"{image_ref} carries no {label} label. Images built from agent-kit "
            "before it was added do not, so the HTTP-contract check is skipped."
        )
        raise ImageLabelMissingError(msg)
    declared = labels[label]
    contracts = tuple(declared.split(",")) if isinstance(declared, str) else ()
    if not contracts or not all(_CONTRACT.fullmatch(c) for c in contracts):
        msg = (
            f"{image_ref} declares {label}={declared!r}, which is not a "
            "comma-separated list of HTTP contracts (e.g. 0.14,0.13). An empty "
            "value means the image build did not expand its build arg; see "
            "agent-kit's `just check-omnigraph-pins`. Rebuild the image."
        )
        raise ValueError(msg)
    return contracts


def validate_http_api_contract(
    server_contract: str,
    witan_contracts: Sequence[str] | None,
    probe_contracts: Sequence[str],
) -> None:
    """Refuse a server contract the running witan or the council probe can't speak.

    Concourse orders nothing between the witan and omnigraph deploy gates. On
    2026-11-04 Production released both together, the 0.14 server finished
    ten minutes before witan started, and the running witan (contract 0.13
    only) failed every graph call for about 15 minutes. witan-core negotiates
    the contract per server, but only a witan already running can negotiate.

    :param server_contract: The deploying server image's contract.
    :param witan_contracts: The contracts the deployed witan image speaks, or
        ``None`` when the witan stack exports none (skipped).
    :param probe_contracts: The council probe's ``SUPPORTED_HTTP_API_CONTRACTS``.
    :raises ValueError: when either can't speak ``server_contract``.
    """
    if server_contract not in probe_contracts:
        msg = (
            f"this omnigraph-server image speaks HTTP contract {server_contract}, "
            "but the council probe (scripts/check_council_health.py "
            f"SUPPORTED_HTTP_API_CONTRACTS) speaks only {','.join(probe_contracts)}, "
            "so every probe run would fail. Add the contract there in the same "
            "change."
        )
        raise ValueError(msg)
    if witan_contracts is None or server_contract in witan_contracts:
        return
    msg = (
        f"this omnigraph-server image speaks HTTP contract {server_contract}, "
        f"but the deployed witan speaks only {','.join(witan_contracts)}. Every "
        "witan graph call would fail from the moment this server rolls until "
        "witan deploys. Release witan's gate first and wait for "
        "deploy-ol-application-witan-<env> to finish. Then re-run "
        "preview-ol-application-omnigraph-<env> (QA, Production) or "
        "deploy-ol-application-omnigraph-ci (CI) by hand: nothing re-triggers "
        "it. See docs/omnigraph-storage-format-upgrade-runbook.md."
    )
    raise ValueError(msg)


def check_http_api_contract(
    server_image: str, witan_contracts: Sequence[str] | None, region: str
) -> str | None:
    """Refuse ``server_image`` when the running witan can't speak its contract.

    The witan side is the witan stack's ``witan_http_api_contracts`` output,
    which that stack reads off its own image while deploying it, rather than a
    read of the running witan image here: ECR's keep-last-N lifecycle rule can
    delete an image that is still running, and a skipped check would then
    open exactly the window this exists to close.

    :param server_image: The omnigraph-server image being deployed.
    :param witan_contracts: The witan stack's ``witan_http_api_contracts``,
        ``None`` before it exports one or when its image is unlabelled.
    :param region: AWS region the ECR repository lives in.
    :returns: Why (part of) the check was skipped, or ``None`` when it all ran.
    :rtype: str | None
    :raises ValueError: when the server's contract isn't spoken, or a label is
        malformed.
    """
    try:
        server = read_image_http_api_contracts(
            server_image, region, HTTP_API_CONTRACT_LABEL
        )
    except ImageSchemaUnavailableError as exc:
        return str(exc)
    if len(server) != 1:
        msg = (
            f"{server_image} declares {HTTP_API_CONTRACT_LABEL}={','.join(server)}; "
            "a server speaks exactly one contract."
        )
        raise ValueError(msg)
    validate_http_api_contract(server[0], witan_contracts, PROBE_HTTP_API_CONTRACTS)
    if witan_contracts is None:
        return (
            "the witan stack exports no witan_http_api_contracts (a deploy that "
            "predates the export, or an image that predates the label)"
        )
    return None
