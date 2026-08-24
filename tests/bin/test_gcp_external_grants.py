"""Tests for the GCP external-grant enumerator.

The interesting surface is credential loading, not the HTTP probes. Four
distinct secret shapes occur in this estate, and a mishandled one fails as an
opaque signing error rather than as "this secret is shaped differently", so each
shape is pinned here.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import sys
from pathlib import Path

import pytest

SCRIPT_PATH = Path(__file__).resolve().parents[2] / "bin" / "gcp-external-grants"

SA_KEY = {
    "client_email": "probe@example.iam.gserviceaccount.com",
    # Not a key, a shape: the tests only check unwrapping and newline unescaping.
    "private_key": "-----BEGIN-----\nAAA\n-----END-----",
    "project_id": "example-project",
}


def load_module():
    """Load the CLI directly; `bin/` is not a package and the file has no suffix."""
    loader = importlib.machinery.SourceFileLoader(
        "test_bin_gcp_external_grants", str(SCRIPT_PATH)
    )
    spec = importlib.util.spec_from_loader("test_bin_gcp_external_grants", loader)
    if spec is None or spec.loader is None:
        msg = f"Unable to load module from {SCRIPT_PATH}"
        raise RuntimeError(msg)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def grants():
    return load_module()


@pytest.mark.parametrize(
    ("body", "field", "description"),
    [
        (SA_KEY, "", "whole secret body is the key (Dagster canvas, edxorg)"),
        (
            {"service_account_creds": json.dumps(SA_KEY)},
            "service_account_creds",
            "one field holding the JSON as a string (xPro)",
        ),
        (
            {"google": {"drive_service_json": json.dumps(SA_KEY)}},
            "google.drive_service_json",
            "nested object (OCW Studio)",
        ),
        (
            {"google": {"drive_service_json": SA_KEY}},
            "google.drive_service_json",
            "nested object already parsed",
        ),
    ],
)
def test_extract_key_handles_every_shape(grants, body, field, description):
    assert grants._extract_key(body, field) == SA_KEY, description


def test_extract_key_names_what_is_actually_there(grants):
    """A wrong field name is the likeliest mistake, so the error must guide."""
    with pytest.raises(grants.ProbeError) as caught:
        grants._extract_key({"service_account_creds": "{}"}, "creds")
    assert "service_account_creds" in str(caught.value)


def test_escaped_pem_is_unescaped(grants):
    """At least one credential stores the PEM with literal backslash-n."""
    escaped = {"private_key": "-----BEGIN-----\\nAAA\\n-----END-----"}
    assert (
        grants._normalise_private_key(escaped)["private_key"]
        == "-----BEGIN-----\nAAA\n-----END-----"
    )


def test_correct_pem_is_left_alone(grants):
    assert grants._normalise_private_key(SA_KEY) == SA_KEY


def _grant(**overrides):
    base = {
        "product": "BigQuery",
        "resource_id": "some-project",
        "resource_name": "Some Project",
        "access": "project-level BigQuery access",
        "granted_by": "project IAM",
        "third_party": False,
    }
    return base | overrides


LEGACY = ""  # legacy gmail-estate projects have no parent at all
MIT_FOLDER = "folder/551004127831"
THEIRS = "folder/249626760288"

PARENTS = {
    "ol-data-platform": LEGACY,
    "ocw-studio-production": LEGACY,
    "mitol01": MIT_FOLDER,
    "mitx-pipeline-main-dc29": THEIRS,
}


@pytest.fixture
def owner(grants, monkeypatch):
    """Ownership seeded from a legacy-estate credential."""
    monkeypatch.setattr(
        grants,
        "_project_parent",
        lambda project_id, _cache: PARENTS.get(project_id, grants.UNKNOWN_PARENT),
    )
    return grants._ownership_for_run([{"project_id": "ol-data-platform"}], (), ())


def test_a_granted_into_project_is_third_party(owner):
    """The core correction: visibility is not ownership.

    `gcloud projects list` returns mitx-pipeline-main-dc29 because OL was granted
    viewer on it, so keying ownership on visibility laundered the grant into
    "internal" -- losing exactly the row this tool exists to surface.
    """
    assert owner.classify_project("mitx-pipeline-main-dc29") is True


def test_legacy_estate_projects_are_ours(owner):
    assert owner.classify_project("ocw-studio-production") is False


def test_an_undescribable_project_is_third_party(owner):
    """No OL identity can read its metadata, so OL does not administer it."""
    assert owner.classify_project("mitir-mitx-surveys") is True


def test_ownership_spans_every_credential_in_the_run(grants, monkeypatch):
    """OL's estate spans two hierarchies; one must not judge the other external."""
    monkeypatch.setattr(
        grants,
        "_project_parent",
        lambda project_id, _cache: PARENTS.get(project_id, grants.UNKNOWN_PARENT),
    )
    legacy_only = grants._ownership_for_run(
        [{"project_id": "ol-data-platform"}], (), ()
    )
    assert legacy_only.classify_project("mitol01") is True

    both = grants._ownership_for_run(
        [{"project_id": "ol-data-platform"}, {"project_id": "mitol01"}], (), ()
    )
    assert both.classify_project("mitol01") is False
    assert both.classify_project("ocw-studio-production") is False
    assert both.classify_project("mitx-pipeline-main-dc29") is True


def test_owned_parent_override_is_honoured(grants, monkeypatch):
    monkeypatch.setattr(
        grants,
        "_project_parent",
        lambda project_id, _cache: PARENTS.get(project_id, grants.UNKNOWN_PARENT),
    )
    pinned = grants._ownership_for_run(
        [{"project_id": "ol-data-platform"}], (MIT_FOLDER,), ()
    )
    assert pinned.classify_project("mitol01") is False


def test_owned_project_override_is_honoured(grants, monkeypatch):
    monkeypatch.setattr(
        grants,
        "_project_parent",
        lambda project_id, _cache: PARENTS.get(project_id, grants.UNKNOWN_PARENT),
    )
    pinned = grants._ownership_for_run(
        [{"project_id": "ol-data-platform"}], (), ("mitx-pipeline-main-dc29",)
    )
    assert pinned.classify_project("mitx-pipeline-main-dc29") is False


def test_service_account_grantors_route_through_the_hierarchy(owner):
    """Every project's SAs share the gserviceaccount.com suffix, ours and theirs."""
    assert (
        owner.classify_email("sa@ocw-studio-production.iam.gserviceaccount.com")
        is False
    )
    assert (
        owner.classify_email("sa@mitx-pipeline-main-dc29.iam.gserviceaccount.com")
        is True
    )


def test_human_grantors_classify_by_domain(owner):
    assert owner.classify_email("someone@edx.org") is True
    assert owner.classify_email("someone@mit.edu") is False
    assert owner.classify_email("someone@sloan.mit.edu") is False
    assert owner.classify_email("someone@gmail.com") is True


def test_no_owned_parents_yields_unknown_not_a_guess(grants, monkeypatch):
    monkeypatch.setattr(
        grants,
        "_project_parent",
        lambda project_id, _cache: PARENTS.get(project_id, ""),
    )
    blind = grants._ownership_for_run([], (), ())
    assert blind.classify_project("ocw-studio-production") is None


def test_duplicate_identities_are_warned_about(grants, capsys):
    """Two sources yielding one identity means another was never probed."""
    grants._warn_on_duplicate_identities(
        [
            {
                "credential": "sa@x",
                "grants": [_grant()],
                "errors": {},
                "source_label": "edx/org path",
            },
            {
                "credential": "sa@x",
                "grants": [_grant()],
                "errors": {},
                "source_label": "google-service-account path",
            },
        ]
    )
    captured = capsys.readouterr().err
    assert "2 sources resolved to sa@x" in captured
    assert "NOT probed" in captured
    assert "edx/org path" in captured


def test_distinct_identities_produce_no_warning(grants, capsys):
    grants._warn_on_duplicate_identities(
        [
            {"credential": "sa@x", "grants": [_grant()], "errors": {}},
            {"credential": "sa@y", "grants": [_grant()], "errors": {}},
        ]
    )
    assert "WARNING" not in capsys.readouterr().err


def test_missing_vault_addr_is_reported_actionably(grants, monkeypatch):
    """A bare KeyError('VAULT_ADDR') tells the operator nothing to do."""
    monkeypatch.delenv("VAULT_ADDR", raising=False)
    with pytest.raises(grants.ProbeError) as caught:
        grants._load_vault("secret-xpro/google-sheets:x")
    assert "VAULT_ADDR" in str(caught.value)
    assert "export" in str(caught.value)


def test_markdown_renders_a_row_per_grant(grants):
    rows = grants._markdown_rows(
        [{"credential": "sa@x", "grants": [_grant()], "errors": {}}]
    )
    assert "| sa@x | BigQuery | `some-project` |" in rows
    assert rows.startswith(grants.MARKDOWN_HEADER)


def test_markdown_marks_third_party_and_unknown_distinctly(grants):
    rows = grants._markdown_rows(
        [
            {
                "credential": "sa@x",
                "grants": [
                    _grant(resource_id="theirs", third_party=True),
                    _grant(resource_id="dunno", third_party=None),
                ],
                "errors": {},
            }
        ]
    )
    assert "| **YES** |" in rows
    assert "| unknown |" in rows


def test_a_credential_that_failed_still_gets_a_row(grants):
    """The whole point: a missing row reads as "asked, found nothing"."""
    rows = grants._markdown_rows(
        [
            {
                "credential": "ol-data-platform-qa@",
                "grants": [],
                "errors": {"load": "VAULT_ADDR is not set"},
            }
        ]
    )
    assert "**NOT ENUMERATED**" in rows
    assert "VAULT_ADDR is not set" in rows


def test_a_credential_with_no_grants_is_not_silently_dropped(grants):
    rows = grants._markdown_rows(
        [{"credential": "sa@quiet", "grants": [], "errors": {}}]
    )
    assert "sa@quiet" in rows
    assert "**NOT ENUMERATED**" in rows


def test_estate_manifest_entries_are_well_formed(grants):
    """probe-all takes no arguments, so a typo here is silent until it runs."""
    assert grants.ESTATE
    for entry in grants.ESTATE:
        assert entry["label"]
        assert ("vault" in entry) ^ ("heroku" in entry)
        if "heroku" in entry:
            app_name, _, variable = entry["heroku"].partition(":")
            assert app_name
            assert variable
        else:
            mount, _, subpath = entry["vault"].partition("/")
            assert mount
            assert subpath


def test_heroku_spec_must_name_an_app_and_a_var(grants):
    with pytest.raises(grants.ProbeError):
        grants._load_heroku("ol-eng-library")


def test_vault_spec_must_name_a_path_under_a_mount(grants, monkeypatch):
    monkeypatch.setenv("VAULT_ADDR", "https://vault.example")
    with pytest.raises(grants.ProbeError):
        grants._load_vault("secret-xpro")
