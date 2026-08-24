"""Tests for the storage-root override used by a storage-format migration.

Every failure mode here is silent, which is why it is validated at config time
and pinned here. ``omnigraph cluster validate`` accepts *any* ``storage:``
string — including an empty one — so nothing downstream rejects a malformed
root. The migration simply rebuilds every graph somewhere nobody is looking and
``load`` reports success on top of it, with the mistake surfacing only at the
verification step, after the outage window.

See ``docs/omnigraph-storage-format-upgrade-runbook.md``.
"""

import pytest

from ol_infrastructure.applications.omnigraph.storage import (
    storage_uri_for,
    validate_internal_schema_version,
    validate_migration_target_prefix,
    validate_storage_prefix,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, ""),
        ("", ""),
        ("   ", ""),
        ("fmt5", "fmt5"),
        ("  fmt5  ", "fmt5"),
        ("fmt10", "fmt10"),
        ("migration-2026-08", "migration-2026-08"),
        ("v2.1", "v2.1"),
        ("a", "a"),
    ],
)
def test_accepts_and_normalizes_valid_prefixes(raw: str | None, expected: str) -> None:
    assert validate_storage_prefix(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "fmt<N>",  # the runbook's placeholder, left unsubstituted
        "<N>",
        "fmt>",
        "fmt 5",  # whitespace inside would make a surprising key
        "fmt/5",  # more than one segment
        "-fmt5",  # must start alphanumeric
        ".fmt5",
        "fmt5!",
        "fmt5$",
    ],
)
def test_rejects_malformed_prefixes(raw: str) -> None:
    with pytest.raises(ValueError, match="storage_prefix"):
        validate_storage_prefix(raw)


@pytest.mark.parametrize("raw", ["/fmt5", "fmt5/", "/fmt5/"])
def test_rejects_slash_wrapped_prefixes(raw: str) -> None:
    """It is joined as ``s3://<bucket>/<prefix>``; a stray slash doubles up."""
    with pytest.raises(ValueError, match="must not start or end with"):
        validate_storage_prefix(raw)


def test_placeholder_rejection_names_the_placeholder() -> None:
    """The error has to point at the actual mistake.

    ``<`` and ``>`` are legal in S3 object keys, so an unsubstituted ``fmt<N>``
    would otherwise become a real prefix rather than an error.
    """
    with pytest.raises(ValueError, match="fmt<N>"):
        validate_storage_prefix("fmt<N>")


def test_storage_uri_without_prefix_is_the_bucket_root() -> None:
    assert storage_uri_for("ol-data-witan-ci", "") == "s3://ol-data-witan-ci"


def test_storage_uri_with_prefix_joins_with_exactly_one_slash() -> None:
    assert storage_uri_for("ol-data-witan-ci", "fmt5") == "s3://ol-data-witan-ci/fmt5"


def test_storage_uri_stays_inside_the_managed_bucket() -> None:
    """The prefix must never be able to redirect to another bucket.

    The bucket, its IAM policy and the IRSA grant are all keyed to the derived
    name, so a root outside it would point the cluster at storage nothing has
    granted access to — and that failure would land mid-migration.
    """
    uri = storage_uri_for("ol-data-witan-production", validate_storage_prefix("fmt5"))
    assert uri.startswith("s3://ol-data-witan-production/")


@pytest.mark.parametrize("raw", ["fmt6", "fmt10", "fmt4"])
def test_migration_target_accepts_fmt_n(raw: str) -> None:
    """The digits are the NEW internal-schema number the rebuild targets."""
    assert validate_migration_target_prefix(raw) == raw


def test_migration_target_unset_is_empty() -> None:
    """Unset is the steady state — no migration armed, so nothing to check."""
    assert validate_migration_target_prefix(None) == ""
    assert validate_migration_target_prefix("  ") == ""


@pytest.mark.parametrize(
    "raw",
    ["v2.1", "migration-2026-08", "fmt", "fmt6a", "6", "FMT6"],
)
def test_migration_target_rejects_anything_but_fmt_n(raw: str) -> None:
    """`validate_storage_prefix` accepts these — a storage root may be named
    anything — but the migration worker hard-requires fmt<N>. Without this
    stricter check they pass preview, arm the outage and suspend the
    maintenance sweeps, only for the Job to refuse the root it was handed.
    """
    with pytest.raises(ValueError, match="fmt<N>"):
        validate_migration_target_prefix(raw)


def test_migration_target_still_rejects_what_the_looser_check_does() -> None:
    """It layers on top of `validate_storage_prefix` rather than replacing it,
    so slashes and unsubstituted placeholders are still caught.
    """
    with pytest.raises(ValueError, match="must not start or end"):
        validate_migration_target_prefix("/fmt6")
    with pytest.raises(ValueError, match="single path segment"):
        validate_migration_target_prefix("fmt<N>")


def test_internal_schema_version_unset_bucket_root_is_fine() -> None:
    """Pre-first-migration steady state: nothing to check either value against."""
    validate_internal_schema_version("", None)


def test_internal_schema_version_matching_fmt_n_passes() -> None:
    validate_internal_schema_version("fmt6", 6)


@pytest.mark.parametrize("prefix", ["fmt6", "fmt10"])
def test_internal_schema_version_missing_against_fmt_n_prefix_fails(
    prefix: str,
) -> None:
    with pytest.raises(ValueError, match="internal_schema_version is unset"):
        validate_internal_schema_version(prefix, None)


def test_internal_schema_version_mismatched_against_fmt_n_prefix_fails() -> None:
    with pytest.raises(ValueError, match="They must agree"):
        validate_internal_schema_version("fmt6", 7)


@pytest.mark.parametrize("prefix", ["", "migration-2026-08", "v2.1"])
def test_internal_schema_version_set_without_fmt_n_prefix_fails(prefix: str) -> None:
    """Nothing to cross-check it against outside the fmt<N> convention."""
    with pytest.raises(ValueError, match="nothing to check it against"):
        validate_internal_schema_version(prefix, 6)
