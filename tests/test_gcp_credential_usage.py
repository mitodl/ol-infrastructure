"""Failure-handling tests for bin/gcp-credential-usage, with no GCP calls."""

import http.client
import importlib.util
import json
import ssl
import subprocess
import sys
import urllib.error
from importlib.machinery import SourceFileLoader
from pathlib import Path
from types import ModuleType
from unittest.mock import Mock

import pytest

PROJECT = "legacy-project"


@pytest.fixture
def script(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """Load the extensionless script without invoking its CLI or gcloud."""
    path = Path(__file__).resolve().parents[1] / "bin" / "gcp-credential-usage"
    loader = SourceFileLoader("gcp_credential_usage", str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, loader.name, module)
    loader.exec_module(module)
    monkeypatch.setattr(module, "_access_token", Mock(return_value="token"))
    return module


@pytest.fixture
def baseline(tmp_path: Path) -> Path:
    path = tmp_path / "baseline.json"
    path.write_text(json.dumps({"projects": [PROJECT], "credentials": []}))
    return path


@pytest.mark.parametrize(
    "error",
    [
        http.client.IncompleteRead(b"partial"),
        http.client.RemoteDisconnected("closed"),
        ConnectionResetError("reset by peer"),
        ssl.SSLError("bad record mac"),
        urllib.error.URLError(TimeoutError("timed out")),
    ],
    ids=lambda error: type(error).__name__,
)
def test_diff_transport_failure_is_unreadable_not_new(
    script: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    baseline: Path,
    error: Exception,
) -> None:
    """Exit 1 means a new credential, so a dropped connection must exit 2."""
    monkeypatch.setattr(script, "_get_json", Mock(side_effect=error))

    with pytest.raises(SystemExit) as exited:
        script.diff(baseline=baseline)

    assert exited.value.code == script.EXIT_UNREADABLE


def test_report_records_a_transport_failure_and_continues(
    script: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    error = urllib.error.URLError(TimeoutError("timed out"))
    monkeypatch.setattr(script, "_get_json", Mock(side_effect=error))

    script.report([PROJECT, "other-project"])

    out = capsys.readouterr().out
    assert f"=== {PROJECT}\n  ERROR: URLError" in out
    assert "=== other-project\n  ERROR: URLError" in out


@pytest.mark.parametrize("command", ["report", "snapshot", "diff"])
def test_failed_project_listing_is_a_readable_error(
    script: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    baseline: Path,
    command: str,
) -> None:
    failure = subprocess.CalledProcessError(
        1, ["gcloud"], stderr="ERROR: reauthentication required\n"
    )
    monkeypatch.setattr(script.subprocess, "run", Mock(side_effect=failure))
    kwargs = {"baseline": baseline} if command == "diff" else {}

    with pytest.raises(SystemExit) as exited:
        getattr(script, command)(all_projects=True, **kwargs)

    assert exited.value.code == script.EXIT_UNREADABLE
    assert "gcloud failed: ERROR: reauthentication required" in capsys.readouterr().err
