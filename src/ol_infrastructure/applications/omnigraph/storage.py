"""Storage-root addressing for the omnigraph cluster.

Deliberately dependency-free — no pulumi, no boto, no ``ol_types``. Importing
``data_tier`` pulls in the EKS component chain, which reaches
``lib/aws/ec2_helper`` and calls boto at import time; that raises
``NoRegionError`` anywhere AWS is not configured, including CI. These two
functions are pure and are the ones worth unit-testing, so they live where a
test can import them without credentials.
"""

import re

# One path segment, starting alphanumeric. Everything else is rejected — see
# validate_storage_prefix for why this is stricter than S3 requires.
_PREFIX_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")

# A migration target root, `fmt<N>` — N being the NEW internal-schema number.
# The worker (scripts/migrate_storage_format.py) enforces the same shape on the
# full URI; this is the same rule applied early enough to fail a `pulumi
# preview` rather than a Job.
_MIGRATION_PREFIX_RE = re.compile(r"fmt[0-9]+")


def validate_storage_prefix(prefix: str | None) -> str:
    """Normalize and check ``omnigraph:storage_prefix``; return "" when unset.

    The storage root is normally the bucket root; a prefix moves it to
    ``s3://<bucket>/<prefix>`` for a storage-format migration (see
    ``docs/omnigraph-storage-format-upgrade-runbook.md``).

    Validated up front because every failure downstream of here is silent:
    ``omnigraph cluster validate`` accepts *any* storage string, including an
    empty one, so a malformed root is never caught by the tooling — the
    migration just rebuilds the graphs somewhere nobody is looking, and
    ``load`` reports success on top of it. ``pulumi preview`` is the only
    place this can fail loudly.

    Raises ``ValueError`` on a leading/trailing ``/`` (it is joined as
    ``s3://<bucket>/<prefix>``) or on anything that is not a single
    ``[A-Za-z0-9._-]`` segment. That last rule is what catches an
    unsubstituted ``fmt<N>`` copied out of the runbook: ``<`` and ``>`` are
    legal in S3 object keys, so it would otherwise become a real prefix.
    """
    cleaned = (prefix or "").strip()
    if not cleaned:
        return ""
    if cleaned.startswith("/") or cleaned.endswith("/"):
        msg = (
            f"omnigraph:storage_prefix must not start or end with '/': "
            f"{cleaned!r}. It is joined as s3://<bucket>/<prefix>."
        )
        raise ValueError(msg)
    if not _PREFIX_RE.fullmatch(cleaned):
        msg = (
            f"omnigraph:storage_prefix must be a single path segment of "
            f"[A-Za-z0-9._-] starting alphanumeric: {cleaned!r}. An "
            "unsubstituted placeholder such as 'fmt<N>' lands here."
        )
        raise ValueError(msg)
    return cleaned


def validate_migration_target_prefix(prefix: str | None) -> str:
    """Normalize and check ``omnigraph:migrate_to_prefix``; return "" when unset.

    STRICTER THAN ``validate_storage_prefix`` ON PURPOSE, and the gap is what
    this exists to close. That one accepts any single path segment, because a
    storage root is free to be named anything. The migration worker, though,
    hard-requires ``fmt<N>`` — the digits are the new internal-schema number,
    and the runbook's guards match on that shape.

    So ``migrate_to_prefix = "v2.1"`` or ``"migration-2026-08"`` passes the
    looser check, survives ``pulumi preview``, arms the outage, suspends the
    maintenance sweeps, and creates a Job whose first act is to refuse the root
    it was given. Failing here instead means it never gets that far.

    Raises ``ValueError`` on anything that is not ``fmt`` followed by digits.
    """
    cleaned = validate_storage_prefix(prefix)
    if not cleaned:
        return ""
    if not _MIGRATION_PREFIX_RE.fullmatch(cleaned):
        msg = (
            f"omnigraph:migrate_to_prefix must be fmt<N> where N is the new "
            f"internal-schema number (e.g. fmt6): {cleaned!r}. The migration "
            "worker rejects anything else, so this would arm an outage for a "
            "Job that cannot run."
        )
        raise ValueError(msg)
    return cleaned


def storage_uri_for(bucket: str, prefix: str) -> str:
    """Cluster storage root for ``bucket``, optionally under ``prefix``.

    Kept separate from the ``Output.apply`` that calls it so the join is
    testable — an off-by-one slash here silently relocates every graph.
    """
    return f"s3://{bucket}/{prefix}" if prefix else f"s3://{bucket}"
