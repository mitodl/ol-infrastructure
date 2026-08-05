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

from ol_infrastructure.applications.omnigraph.data_tier import (
    storage_uri_for,
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
