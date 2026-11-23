"""The core stack's Tika image tag: read from the chart index, with a fallback."""

import importlib
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest
import requests

from bridge.lib.versions import TIKA_CHART_VERSION

REPO = Path(__file__).parents[2]
INDEX = f"""\
apiVersion: v1
entries:
  tika:
  - version: {TIKA_CHART_VERSION}
    appVersion: from-index-full
  - version: 0.0.1
    appVersion: other
"""


class _Response:
    def __init__(self, text: str, status: int = 200) -> None:
        self.text = text
        self.status = status

    def raise_for_status(self) -> None:
        if self.status >= 400:
            msg = f"HTTP {self.status}"
            raise requests.HTTPError(msg)


@pytest.fixture
def search(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """Import the core stack's search module as it does, and record warnings."""
    monkeypatch.syspath_prepend(str(REPO / "local-dev/infra"))
    module = importlib.import_module("modules.search")
    warnings: list[str] = []
    monkeypatch.setattr(module, "log", SimpleNamespace(warn=warnings.append))
    monkeypatch.setattr(module, "warnings", warnings, raising=False)
    return module


def _serve(
    monkeypatch: pytest.MonkeyPatch, module: ModuleType, outcome: Any
) -> list[str]:
    """Make requests.get return ``outcome``, or raise it if it is an exception."""
    urls: list[str] = []

    def fake_get(url: str, timeout: int) -> _Response:  # noqa: ARG001
        urls.append(url)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(module.requests, "get", fake_get)
    return urls


def test_fallback_follows_the_chart_version(search):
    # Renovate bumps TIKA_CHART_VERSION alone, so this fails that PR until the
    # fallback moves too. Since chart 2.7.0 every chart X.Y.Z has run an image
    # tagged X.Y.Z.<n>, with or without -full.
    assert search.TIKA_FALLBACK_IMAGE_TAG.startswith(f"{TIKA_CHART_VERSION}."), (
        "Set TIKA_FALLBACK_IMAGE_TAG in local-dev/infra/modules/search.py to the "
        f"appVersion of Tika chart {TIKA_CHART_VERSION}: helm show chart tika "
        f"--repo {search.TIKA_CHART_REPO} --version {TIKA_CHART_VERSION} "
        "| grep appVersion"
    )


def test_reads_the_tag_from_the_chart_index(search, monkeypatch):
    urls = _serve(monkeypatch, search, _Response(INDEX))

    assert search.tika_image_tag() == "from-index-full"
    assert urls == [f"{search.TIKA_CHART_REPO}/index.yaml"]
    assert search.warnings == []


@pytest.mark.parametrize(
    "outcome",
    [
        # What a stalled link raised on 2026-11-19.
        requests.ReadTimeout("read timed out"),
        requests.ConnectionError("no route to host"),
        _Response("", status=503),
        _Response("entries: [unclosed"),
        _Response(""),
        _Response(INDEX.replace(TIKA_CHART_VERSION, "9.9.9")),
    ],
    ids=["timeout", "offline", "http-error", "bad-yaml", "empty", "no-entry"],
)
def test_falls_back_when_the_index_cannot_be_read(search, monkeypatch, outcome):
    _serve(monkeypatch, search, outcome)

    assert search.tika_image_tag() == search.TIKA_FALLBACK_IMAGE_TAG
    assert len(search.warnings) == 1
    assert search.TIKA_FALLBACK_IMAGE_TAG in search.warnings[0]
