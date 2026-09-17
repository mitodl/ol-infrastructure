"""Failure-path tests for the GCP external-grant enumerator.

`probe-all`'s contract is that no credential is skipped silently. A traceback
mid-run breaks that harder than a missing row does: every credential after the
crash is lost, and the partial output looks like a complete one. So every
external failure -- a missing binary, an unreadable file, a non-JSON error body,
a Vault error -- has to arrive as a reported `ProbeError` rather than an
exception nobody catches.

Each test here corresponds to a crash path found in review of PR #5577.
"""

from __future__ import annotations

import pytest

from tests.bin.test_gcp_external_grants import load_module


@pytest.fixture(scope="module")
def grants():
    return load_module()


def test_missing_binary_becomes_a_probe_error(grants):
    """Gcloud or sops not being installed must not abort the run."""
    with pytest.raises(grants.ProbeError) as caught:
        grants._run(["definitely-not-a-real-binary-xyz", "--version"])
    assert "definitely-not-a-real-binary-xyz" in str(caught.value)


def test_unreadable_key_file_becomes_a_probe_error(grants, tmp_path):
    with pytest.raises(grants.ProbeError):
        grants._load_key_file(tmp_path / "does-not-exist.json")


def test_malformed_key_file_becomes_a_probe_error(grants, tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    with pytest.raises(grants.ProbeError):
        grants._load_key_file(bad)


def test_non_json_error_body_does_not_crash(grants, monkeypatch):
    """A gateway returning HTML on a 502 must surface as HTTP 502, not ValueError."""

    class Response:
        ok = False
        status_code = 502
        reason = "Bad Gateway"
        content = b"<html>oops</html>"

        def json(self):
            message = "not json"
            raise ValueError(message)

    monkeypatch.setattr(grants.requests, "get", lambda *_a, **_k: Response())
    with pytest.raises(grants.ProbeError) as caught:
        grants._get("https://example.test/x", "token")
    assert "502" in str(caught.value)


def test_non_json_success_body_does_not_crash(grants, monkeypatch):
    class Response:
        ok = True

        def json(self):
            message = "not json"
            raise ValueError(message)

    monkeypatch.setattr(grants.requests, "get", lambda *_a, **_k: Response())
    with pytest.raises(grants.ProbeError):
        grants._get("https://example.test/x?a=b", "token")


def test_vault_v1_failure_surfaces_as_probe_error(grants):
    """Hvac's own exception type appears in no caller's except clause."""

    class V1:
        @staticmethod
        def read_secret(**_kwargs):
            raise grants.hvac.exceptions.VaultError

    class V2:
        @staticmethod
        def read_secret_version(**_kwargs):
            raise grants.hvac.exceptions.VaultError

    class Kv:
        v1 = V1()
        v2 = V2()

    class Secrets:
        kv = Kv()

    class Broken:
        secrets = Secrets()

    with pytest.raises(grants.ProbeError) as caught:
        grants._read_vault_secret(Broken(), "secret-x", "path/y")
    assert "secret-x/path/y" in str(caught.value)


def test_gcloud_token_failure_is_reported_not_raised(grants, monkeypatch):
    """--gcloud on a machine with no active login must not kill the run."""

    def failing(_command):
        message = "cannot run 'gcloud'"
        raise grants.ProbeError(message)

    monkeypatch.setattr(grants, "_run", failing)
    result = grants._probe_one({"_gcloud": True}, grants.Ownership(set(), set()))
    assert result["errors"]["token"]
    assert result["grants"] == []


def test_duplicates_are_detected_even_when_every_probe_errored(grants, capsys):
    """Two sources reaching one credential still means one went unprobed.

    The earlier condition required grants-or-no-errors, so a duplicate whose
    probes all failed produced no warning and no rows -- invisible twice over.
    """
    grants._warn_on_duplicate_identities(
        [
            {
                "credential": "sa@x",
                "grants": [],
                "errors": {"bigquery": "403"},
                "source_label": "first source",
            },
            {
                "credential": "sa@x",
                "grants": [],
                "errors": {"bigquery": "403"},
                "source_label": "second source",
            },
        ]
    )
    assert "2 sources resolved to sa@x" in capsys.readouterr().err


def test_load_failures_are_not_counted_as_duplicates(grants, capsys):
    """Two sources that never resolved to an identity are not one credential."""
    grants._warn_on_duplicate_identities(
        [
            {
                "credential": "vault/a",
                "grants": [],
                "errors": {"load": "no such path"},
                "source_label": "a",
            },
            {
                "credential": "vault/b",
                "grants": [],
                "errors": {"load": "no such path"},
                "source_label": "b",
            },
        ]
    )
    assert "WARNING" not in capsys.readouterr().err


@pytest.mark.parametrize("probe_name", ["_probe_analytics", "_probe_youtube"])
def test_probes_follow_pagination(grants, monkeypatch, probe_name):
    """Silent truncation is indistinguishable from a complete answer."""
    calls = []

    def fake_get(_url, _token, **params):
        calls.append(params.get("pageToken"))
        if params.get("pageToken"):
            return {"accountSummaries": [], "items": []}
        return {"accountSummaries": [], "items": [], "nextPageToken": "page2"}

    monkeypatch.setattr(grants, "_get", fake_get)
    getattr(grants, probe_name)("token", grants.Ownership(set(), set()))
    assert calls == [None, "page2"]
