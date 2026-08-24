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


OWNED = {"ol-data-platform", "mitxpro"}


def test_human_addresses_classify_by_domain(grants):
    assert grants._is_third_party_email("someone@edx.org", OWNED)
    assert not grants._is_third_party_email("someone@mit.edu", OWNED)
    assert not grants._is_third_party_email("someone@sloan.mit.edu", OWNED)


def test_service_account_addresses_classify_by_project(grants):
    """Every project's SAs share the gserviceaccount.com suffix, ours and theirs.

    Judging these by domain would silently mark a third party's service account
    as OL-owned -- which is the direction of error that loses a grant.
    """
    assert not grants._is_third_party_email(
        "sa@ol-data-platform.iam.gserviceaccount.com", OWNED
    )
    assert grants._is_third_party_email(
        "sa@some-edx-project.iam.gserviceaccount.com", OWNED
    )


def test_ownership_is_unknown_without_a_project_list(grants):
    """An empty owned-set means gcloud failed; that must not read as an answer."""
    assert (
        grants._is_third_party_email("sa@anything.iam.gserviceaccount.com", set())
        is None
    )
    assert grants._is_third_party_email("", OWNED) is None


def test_capability_summary_ranks_access(grants):
    assert grants._capability_summary({"canManageMembers": True}) == "organizer"
    assert grants._capability_summary({"canEdit": True}) == "writer"
    assert grants._capability_summary({"canComment": True}) == "commenter"
    assert grants._capability_summary({}) == "reader"
