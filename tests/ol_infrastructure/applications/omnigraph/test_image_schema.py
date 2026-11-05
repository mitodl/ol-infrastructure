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
    HTTP_API_CONTRACT_LABEL,
    HTTP_API_CONTRACTS_LABEL,
    IMAGE_SCHEMA_LABEL,
    MIN_IMAGE_SCHEMA_LABEL,
    ImageLabelMissingError,
    ImageSchemaUnavailableError,
    _image_id,
    check_http_api_contract,
    read_image_http_api_contracts,
    read_image_internal_schema_range,
    repository_name,
    validate_http_api_contract,
)

_REPOSITORY = "omnigraph-server"
_REGISTRY = "610119931565.dkr.ecr.us-east-1.amazonaws.com"
_DIGEST = "sha256:" + "a" * 64
_CONFIG_DIGEST = "sha256:" + "b" * 64
_BY_DIGEST = f"{_REGISTRY}/{_REPOSITORY}@{_DIGEST}"


class _FakeEcr:
    """The two ECR calls the reader makes, answered from canned manifests.

    A manifest value may be a ``str``, which is returned verbatim, so a test
    can hand the reader something that is not JSON at all.
    """

    def __init__(
        self,
        manifests: dict[str, dict[str, Any] | str],
        download_url: str = "https://blob",
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
        manifest = self.manifests[key]
        raw = manifest if isinstance(manifest, str) else json.dumps(manifest)
        return {"images": [{"imageManifest": raw}], "failures": []}

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
    assert read_image_internal_schema_range(_REPOSITORY, _BY_DIGEST, "us-east-1") == (
        9,
        9,
    )


def test_reads_the_oldest_format_served_in_place(patched) -> None:
    """Omnigraph 0.14 writes 16 and opens a format-14 root without a rebuild."""
    client = _FakeEcr({_DIGEST: _image_manifest()})
    patched(
        client,
        {
            "config": {
                "Labels": {IMAGE_SCHEMA_LABEL: "16", MIN_IMAGE_SCHEMA_LABEL: "14"}
            }
        },
    )
    assert read_image_internal_schema_range(_REPOSITORY, _BY_DIGEST, "us-east-1") == (
        14,
        16,
    )


def test_a_present_but_unparseable_min_label_fails_the_preview(patched) -> None:
    """Same rule as the written-format label: an empty value is a broken build."""
    client = _FakeEcr({_DIGEST: _image_manifest()})
    patched(
        client,
        {"config": {"Labels": {IMAGE_SCHEMA_LABEL: "16", MIN_IMAGE_SCHEMA_LABEL: ""}}},
    )
    with pytest.raises(ValueError, match="OMNIGRAPH_MIN_INTERNAL_SCHEMA") as caught:
        read_image_internal_schema_range(_REPOSITORY, _BY_DIGEST, "us-east-1")
    assert not isinstance(caught.value, ImageSchemaUnavailableError)


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
    assert read_image_internal_schema_range(_REPOSITORY, _BY_DIGEST, "us-east-1") == (
        9,
        9,
    )
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
        read_image_internal_schema_range(_REPOSITORY, _BY_DIGEST, "us-east-1")


def test_missing_image_is_unavailable_not_fatal(patched) -> None:
    """A digest ECR has never heard of, e.g. one a lifecycle sweep removed."""
    client = _FakeEcr({})
    patched(client, {})
    with pytest.raises(ImageSchemaUnavailableError, match="no manifest"):
        read_image_internal_schema_range(_REPOSITORY, _BY_DIGEST, "us-east-1")


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
        read_image_internal_schema_range(_REPOSITORY, _BY_DIGEST, "us-east-1")


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
        read_image_internal_schema_range(_REPOSITORY, _BY_DIGEST, "us-east-1")
    assert not isinstance(caught.value, ImageSchemaUnavailableError)


# ── malformed registry data must skip, never abort an unrelated preview ──────
# The non-fatality promise is absolute, so each of these is a path that used to
# escape as JSONDecodeError, KeyError or TypeError from three frames down into
# a `pulumi preview` that has nothing to do with a storage-format migration.


def test_an_unparseable_manifest_is_unavailable_not_fatal(patched) -> None:
    """A registry serving something that is not JSON."""
    client = _FakeEcr({_DIGEST: "<html>502 Bad Gateway</html>"})
    patched(client, {})
    with pytest.raises(ImageSchemaUnavailableError, match="unparseable manifest"):
        read_image_internal_schema_range(_REPOSITORY, _BY_DIGEST, "us-east-1")


def test_an_index_entry_without_a_digest_is_unavailable_not_fatal(patched) -> None:
    """Nothing to follow, and no KeyError out of the walk."""
    index = {
        "manifests": [{"platform": {"os": "linux", "architecture": "amd64"}}],
    }
    client = _FakeEcr({_DIGEST: index})
    patched(client, {})
    with pytest.raises(ImageSchemaUnavailableError, match=r"carries no\s+digest"):
        read_image_internal_schema_range(_REPOSITORY, _BY_DIGEST, "us-east-1")


@pytest.mark.parametrize(
    "config_blob",
    [
        {},  # `config` is optional in the OCI image-config spec
        {"config": None},
        {"config": {"Labels": "not-a-mapping"}},
    ],
)
def test_a_config_blob_of_an_unexpected_shape_is_unavailable_not_fatal(
    patched,
    config_blob: dict[str, Any],
) -> None:
    """Indexing straight into the blob would raise KeyError or TypeError."""
    client = _FakeEcr({_DIGEST: _image_manifest()})
    patched(client, config_blob)
    with pytest.raises(ImageSchemaUnavailableError):
        read_image_internal_schema_range(_REPOSITORY, _BY_DIGEST, "us-east-1")


def test_a_non_string_label_value_gets_the_actionable_message(patched) -> None:
    """``int(None)`` raises TypeError, which the ValueError-only catch missed."""
    client = _FakeEcr({_DIGEST: _image_manifest()})
    patched(client, {"config": {"Labels": {IMAGE_SCHEMA_LABEL: ["9"]}}})
    with pytest.raises(ValueError, match="not an integer storage-format"):
        read_image_internal_schema_range(_REPOSITORY, _BY_DIGEST, "us-east-1")


@pytest.mark.parametrize(
    "index",
    [
        {"manifests": None},
        {"manifests": "not-a-list"},
        {"manifests": [None]},
        {"manifests": [{"platform": None}]},
        {"manifests": [{"platform": "linux/amd64"}]},
    ],
)
def test_a_malformed_index_is_unavailable_not_fatal(
    patched,
    index: dict[str, Any],
) -> None:
    """JSON has no schema, so a key can hold the wrong type rather than nothing.

    ``.get`` on a ``None`` raises AttributeError, and iterating one raises
    TypeError; either would abort a preview that has nothing to do with a
    storage-format migration.
    """
    client = _FakeEcr({_DIGEST: index})
    patched(client, {})
    with pytest.raises(ImageSchemaUnavailableError):
        read_image_internal_schema_range(_REPOSITORY, _BY_DIGEST, "us-east-1")


@pytest.mark.parametrize(
    "manifest",
    [{"config": None}, {"config": "sha256:abc"}, {}],
)
def test_a_manifest_without_a_usable_config_is_unavailable_not_fatal(
    patched,
    manifest: dict[str, Any],
) -> None:
    """No config blob to fetch, and no KeyError or TypeError out of the lookup."""
    client = _FakeEcr({_DIGEST: manifest})
    patched(client, {})
    with pytest.raises(ImageSchemaUnavailableError, match="image config"):
        read_image_internal_schema_range(_REPOSITORY, _BY_DIGEST, "us-east-1")


@pytest.mark.parametrize("config_blob", [None, "a string", ["a", "list"]])
def test_a_non_object_config_blob_is_unavailable_not_fatal(
    patched,
    config_blob: object,
) -> None:
    """A presigned URL that serves something other than an image config."""
    client = _FakeEcr({_DIGEST: _image_manifest()})
    patched(client, config_blob)
    with pytest.raises(ImageSchemaUnavailableError, match="carries no"):
        read_image_internal_schema_range(_REPOSITORY, _BY_DIGEST, "us-east-1")


# ── HTTP-contract labels: the deploy-order check ─────────────────────────────


@pytest.mark.parametrize(
    "image_ref",
    [f"{_REGISTRY}/witan@{_DIGEST}", f"{_REGISTRY}/witan:abc1234"],
)
def test_repository_name_from_either_ref_shape(image_ref: str) -> None:
    assert repository_name(image_ref) == "witan"


@pytest.mark.parametrize(
    ("label", "declared", "expected"),
    [
        (HTTP_API_CONTRACT_LABEL, "0.14", ("0.14",)),
        (HTTP_API_CONTRACTS_LABEL, "0.14,0.13", ("0.14", "0.13")),
    ],
)
def test_reads_the_contract_labels(
    patched, label: str, declared: str, expected: tuple[str, ...]
) -> None:
    client = _FakeEcr({_DIGEST: _image_manifest()})
    patched(client, {"config": {"Labels": {label: declared}}})
    assert read_image_http_api_contracts(_BY_DIGEST, "us-east-1", label) == expected


def test_an_image_without_the_contract_label_is_unavailable_not_fatal(
    patched,
) -> None:
    """Every image built before agent-kit added the label, rollbacks included."""
    client = _FakeEcr({_DIGEST: _image_manifest()})
    patched(client, {"config": {"Labels": {IMAGE_SCHEMA_LABEL: "16"}}})
    with pytest.raises(ImageLabelMissingError, match="carries no"):
        read_image_http_api_contracts(_BY_DIGEST, "us-east-1", HTTP_API_CONTRACT_LABEL)


def test_an_unreadable_registry_is_not_a_missing_label(patched) -> None:
    """The witan stack exports null only for a missing label and fails on this.

    A registry blip during a witan deploy must not switch the omnigraph check
    off until the next witan deploy.
    """
    patched(_FakeEcr({}), {})
    with pytest.raises(ImageSchemaUnavailableError) as caught:
        read_image_http_api_contracts(_BY_DIGEST, "us-east-1", HTTP_API_CONTRACTS_LABEL)
    assert not isinstance(caught.value, ImageLabelMissingError)


@pytest.mark.parametrize("declared", ["", "0.14,", "latest", 14])
def test_a_present_but_malformed_contract_label_fails_the_preview(
    patched, declared: object
) -> None:
    """An empty label is an unexpanded build arg, not an old image."""
    client = _FakeEcr({_DIGEST: _image_manifest()})
    patched(client, {"config": {"Labels": {HTTP_API_CONTRACTS_LABEL: declared}}})
    with pytest.raises(ValueError, match="not a comma-separated list") as caught:
        read_image_http_api_contracts(_BY_DIGEST, "us-east-1", HTTP_API_CONTRACTS_LABEL)
    assert not isinstance(caught.value, ImageSchemaUnavailableError)


_PROBE = ("0.14", "0.13")


def test_a_witan_that_negotiates_the_server_contract_passes() -> None:
    validate_http_api_contract("0.14", ["0.14", "0.13"], _PROBE)


def test_a_server_contract_the_witan_cannot_speak_is_refused() -> None:
    """The 2026-11-04 Production shape: a 0.14 server ahead of a 0.13-only witan."""
    with pytest.raises(ValueError, match="Release witan's gate first") as caught:
        validate_http_api_contract("0.14", ["0.13"], _PROBE)
    # QA and Production refuse in the preview job, so that's what to re-run.
    assert "preview-ol-application-omnigraph-<env>" in str(caught.value)


def test_a_server_contract_the_council_probe_cannot_speak_is_refused() -> None:
    with pytest.raises(ValueError, match="council probe"):
        validate_http_api_contract("0.15", ["0.15", "0.14"], _PROBE)


def test_the_probe_is_checked_even_when_witan_exports_nothing() -> None:
    with pytest.raises(ValueError, match="council probe"):
        validate_http_api_contract("0.15", None, _PROBE)


def _server_label(monkeypatch: pytest.MonkeyPatch, contracts: tuple[str, ...]) -> None:
    monkeypatch.setattr(
        "ol_infrastructure.applications.omnigraph.image_schema."
        "read_image_http_api_contracts",
        lambda _ref, _region, label: (
            contracts if label == HTTP_API_CONTRACT_LABEL else pytest.fail(label)
        ),
    )


def test_check_skips_when_witan_exports_no_contracts(monkeypatch) -> None:
    _server_label(monkeypatch, ("0.14",))
    reason = check_http_api_contract(_BY_DIGEST, None, "us-east-1")
    assert reason is not None
    assert "witan_http_api_contracts" in reason


def test_check_skips_on_an_unlabelled_server_image(patched) -> None:
    client = _FakeEcr({_DIGEST: _image_manifest()})
    patched(client, {"config": {"Labels": {}}})
    reason = check_http_api_contract(_BY_DIGEST, ["0.14"], "us-east-1")
    assert reason is not None
    assert "carries no" in reason


def test_check_refuses_a_mismatch_without_reading_the_witan_image(
    monkeypatch,
) -> None:
    """Only the server image is read from ECR; witan's list comes from its stack."""
    _server_label(monkeypatch, ("0.14",))
    with pytest.raises(ValueError, match=r"speaks only 0\.13"):
        check_http_api_contract(_BY_DIGEST, ["0.13"], "us-east-1")


def test_a_server_label_naming_two_contracts_fails_the_preview(monkeypatch) -> None:
    _server_label(monkeypatch, ("0.14", "0.13"))
    with pytest.raises(ValueError, match="exactly one contract"):
        check_http_api_contract(_BY_DIGEST, ["0.14"], "us-east-1")
