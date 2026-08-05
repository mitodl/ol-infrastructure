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


def storage_uri_for(bucket: str, prefix: str) -> str:
    """Cluster storage root for ``bucket``, optionally under ``prefix``.

    Kept separate from the ``Output.apply`` that calls it so the join is
    testable — an off-by-one slash here silently relocates every graph.
    """
    return f"s3://{bucket}/{prefix}" if prefix else f"s3://{bucket}"
