"""Tests for reading an image's declared storage format out of ECR.

The value of this module is entirely in what it does when the read does NOT
work. Every failure is deliberately non-fatal so a rollback to a pre-label
image, or a run without ECR access, still deploys. The one exception is a
label that is present but not an integer, which means the image build itself
is broken and must not be treated as "no label" — that is how the gate would
get switched off with nothing failing anywhere.
"""

import json
from typing import Any

import pytest

from ol_infrastructure.applications.omnigraph.image_schema import (
    IMAGE_SCHEMA_LABEL,
    ImageSchemaUnavailableError,
    _image_id,
    read_image_internal_schema,
)

_REPOSITORY = "omnigraph-server"
_REGISTRY = "610119931565.dkr.ecr.us-east-1.amazonaws.com"
_DIGEST = "sha256:" + "a" * 64
_CONFIG_DIGEST = "sha256:" + "b" * 64
_BY_DIGEST = f"{_REGISTRY}/{_REPOSITORY}@{_DIGEST}"


class _FakeEcr:
    """The two ECR calls the reader makes, answered from canned manifests."""

    def __init__(
        self, manifests: dict[str, dict[str, Any]], download_url: str = "https://blob"
    ) -> None:
        self.manifests = manifests
        self.download_url = download_url
        self.requested: list[dict[str, str]] = []

    def batch_get_image(
        self,
        repositoryName: str,
        imageIds: list[dict[str, str]],
        **_: Any,
    ) -> dict[str, Any]:
        assert repositoryName == _REPOSITORY
        image_id = imageIds[0]
        self.requested.append(image_id)
        key = next(iter(image_id.values()))
        if key not in self.manifests:
            return {"images": [], "failures": [{"failureCode": "ImageNotFound"}]}
        return {
            "images": [{"imageManifest": json.dumps(self.manifests[key])}],
            "failures": [],
        }

    def get_download_url_for_layer(
        self,
        repositoryName: str,
        layerDigest: str,
        **_: Any,
    ) -> dict[str, str]:
        assert repositoryName == _REPOSITORY
        assert layerDigest == _CONFIG_DIGEST
        return {"downloadUrl": self.download_url}


def _image_manifest() -> dict[str, Any]:
    return {
        "schemaVersion": 2,
        "mediaType": "application/vnd.docker.distribution.manifest.v2+json",
        "config": {"digest": _CONFIG_DIGEST},
        "layers": [],
    }


@pytest.fixture
def patched(monkeypatch: pytest.MonkeyPatch):
    """Install a fake ECR client and a fake config-blob fetch.

    Returns a callable taking the fake client and the config-blob document.
    """

    def install(client: _FakeEcr, config_blob: dict[str, Any]) -> None:
        monkeypatch.setattr(
            "ol_infrastructure.applications.omnigraph.image_schema.boto3.client",
            lambda *_args, **_kwargs: client,
        )

        class _Response:
            @staticmethod
            def json() -> dict[str, Any]:
                return config_blob

        monkeypatch.setattr(
            "ol_infrastructure.applications.omnigraph.image_schema.requests.get",
            lambda *_args, **_kwargs: _Response(),
        )

    return install


@pytest.mark.parametrize(
    ("image_ref", "expected"),
    [
        (_BY_DIGEST, {"imageDigest": _DIGEST}),
        (f"{_REGISTRY}/{_REPOSITORY}:abc1234", {"imageTag": "abc1234"}),
    ],
)
def test_image_id_handles_both_shapes_format_docker_image_ref_produces(
    image_ref: str, expected: dict[str, str]
) -> None:
    """A digest pin from the pipeline, or a tag from a local run."""
    assert _image_id(image_ref) == expected


def test_image_id_rejects_a_bare_repository() -> None:
    """A ref with neither digest nor tag names no single image.

    ``rpartition`` would otherwise read the registry host's ``.com`` fragment
    as a tag and ask ECR a nonsense question.
    """
    with pytest.raises(ImageSchemaUnavailableError, match="neither a digest nor a tag"):
        _image_id(f"{_REGISTRY}/{_REPOSITORY}")


def test_reads_the_label_off_a_plain_manifest(patched) -> None:
    """The shape the Concourse build actually pushes today."""
    client = _FakeEcr({_DIGEST: _image_manifest()})
    patched(client, {"config": {"Labels": {IMAGE_SCHEMA_LABEL: "9"}}})
    assert read_image_internal_schema(_REPOSITORY, _BY_DIGEST, "us-east-1") == 9


def test_walks_a_multi_platform_index_to_the_linux_amd64_manifest(patched) -> None:
    """Attestation entries sit in the index too, with architecture "unknown".

    Taking the first entry would read an attestation's config, which carries no
    labels, and report the image as unlabelled.
    """
    child = "sha256:" + "c" * 64
    index = {
        "mediaType": "application/vnd.oci.image.index.v1+json",
        "manifests": [
            {
                "digest": "sha256:" + "d" * 64,
                "platform": {"os": "unknown", "architecture": "unknown"},
            },
            {"digest": child, "platform": {"os": "linux", "architecture": "amd64"}},
        ],
    }
    client = _FakeEcr({_DIGEST: index, child: _image_manifest()})
    patched(client, {"config": {"Labels": {IMAGE_SCHEMA_LABEL: "9"}}})
    assert read_image_internal_schema(_REPOSITORY, _BY_DIGEST, "us-east-1") == 9
    assert client.requested[-1] == {"imageDigest": child}


def test_index_without_linux_amd64_is_unavailable_not_fatal(patched) -> None:
    """Nothing to read a config blob from, but still not a reason to refuse."""
    index = {
        "manifests": [
            {
                "digest": "sha256:" + "d" * 64,
                "platform": {"os": "linux", "architecture": "arm64"},
            }
        ]
    }
    client = _FakeEcr({_DIGEST: index})
    patched(client, {})
    with pytest.raises(ImageSchemaUnavailableError, match="no linux/amd64"):
        read_image_internal_schema(_REPOSITORY, _BY_DIGEST, "us-east-1")


def test_missing_image_is_unavailable_not_fatal(patched) -> None:
    """A digest ECR has never heard of, e.g. one a lifecycle sweep removed."""
    client = _FakeEcr({})
    patched(client, {})
    with pytest.raises(ImageSchemaUnavailableError, match="no manifest"):
        read_image_internal_schema(_REPOSITORY, _BY_DIGEST, "us-east-1")


@pytest.mark.parametrize(
    "config_blob",
    [
        {"config": {"Labels": None}},
        {"config": {}},
        {"config": {"Labels": {"org.opencontainers.image.version": "0.11.0"}}},
    ],
)
def test_an_image_without_the_label_is_unavailable_not_fatal(
    patched,
    config_blob: dict[str, Any],
) -> None:
    """Every pre-label image lands here, including on a rollback.

    ``Labels`` comes back as JSON null, not absent, on an image that declares
    none, so both spellings have to read as "no label".
    """
    client = _FakeEcr({_DIGEST: _image_manifest()})
    patched(client, config_blob)
    with pytest.raises(ImageSchemaUnavailableError, match="carries no"):
        read_image_internal_schema(_REPOSITORY, _BY_DIGEST, "us-east-1")


@pytest.mark.parametrize("declared", ["", "nine", "9.0"])
def test_a_present_but_unparseable_label_fails_the_preview(
    patched,
    declared: str,
) -> None:
    """A broken build must not read as an old image.

    The empty string is how this happens in practice: a Dockerfile runtime
    stage that does not re-declare the build arg expands the label to "" and
    still builds a clean image. Treating that as "no label" would silently
    switch the gate off, so it raises ValueError rather than the skippable
    ImageSchemaUnavailableError.
    """
    client = _FakeEcr({_DIGEST: _image_manifest()})
    patched(client, {"config": {"Labels": {IMAGE_SCHEMA_LABEL: declared}}})
    with pytest.raises(ValueError, match="not an integer storage-format") as caught:
        read_image_internal_schema(_REPOSITORY, _BY_DIGEST, "us-east-1")
    assert not isinstance(caught.value, ImageSchemaUnavailableError)
