"""Tests for docker_image_config_kwargs, the image kwargs every k8s app unpacks."""

import pytest

from ol_infrastructure.lib.pulumi_helper import docker_image_config_kwargs

DIGEST = "sha256:" + "a" * 64


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for suffix in ("DOCKER_TAG", "DOCKER_SHA", "RELEASE_VERSION"):
        monkeypatch.delenv(f"MYAPP_{suffix}", raising=False)


def test_digest_with_release_version(monkeypatch):
    """The release-workflow QA/Production shape: pinned digest, labelled version."""
    monkeypatch.setenv("MYAPP_DOCKER_SHA", DIGEST)
    monkeypatch.setenv("MYAPP_RELEASE_VERSION", "2026.10.28.1")

    assert docker_image_config_kwargs("MYAPP") == {
        "application_image_digest": DIGEST,
        "application_version": "2026.10.28.1",
    }


def test_no_release_version_adds_nothing(monkeypatch):
    """CI and the legacy pipelines set no version, so their config is unchanged."""
    monkeypatch.setenv("MYAPP_DOCKER_TAG", "abc123")

    assert docker_image_config_kwargs("MYAPP") == {"application_docker_tag": "abc123"}


def test_tag_and_digest_together_are_still_rejected(monkeypatch):
    """The version travels in its own variable, not as a second image ref."""
    monkeypatch.setenv("MYAPP_DOCKER_TAG", "2026.10.28.1")
    monkeypatch.setenv("MYAPP_DOCKER_SHA", DIGEST)

    with pytest.raises(OSError, match="Cannot set both"):
        docker_image_config_kwargs("MYAPP")
