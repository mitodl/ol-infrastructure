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

# Same shape as _MIGRATION_PREFIX_RE, but capturing — used to pull the digits
# back out of a served `storage_prefix` that already follows the convention,
# for validate_internal_schema_version below.
_PREFIX_SCHEMA_RE = re.compile(r"fmt(?P<version>[0-9]+)")


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


def validate_internal_schema_version(
    storage_prefix: str, internal_schema_version: int | None
) -> None:
    """Cross-check ``omnigraph:internal_schema_version`` against ``storage_prefix``.

    Neither value derives the other — auto-deriving one from the other was
    considered and rejected, same as the migration cutover itself:
    ``storage_prefix`` is the switch a human flips only after a rebuild is
    verified, not something to recompute on every deploy. This is a pure
    cross-check between two independently-set config values that a real
    migration touches together, so a slip in one without the other is caught
    here — at ``pulumi preview`` — rather than downstream, where it is
    silent: the server just refuses the store and crashloops.

    Deliberately narrower than it sounds, and worth stating precisely: this
    does NOT verify either value against the image actually being deployed,
    or against the live store's own ``internal_schema_version``
    (``omnigraph snapshot``). ol-infrastructure has no access to agent-kit's
    version -> internal-schema mapping, and the deploying image is addressed
    only by digest (``OMNIGRAPH_DOCKER_SHA``), never a semver string that
    could be looked up. What this closes is the narrower, still-real gap of
    a human editing one of a *committed* pair of config values without the
    other — the exact category of mistake the CI incident behind this check
    was (``storage_prefix`` reverted while the deployed image could only
    read the new format). See
    tk-derive-the-s3-storage-prefix-from-the-binary-s-i-bc3385 for the wider
    gap this leaves open.

    Raises ``ValueError`` when ``storage_prefix`` follows the ``fmt<N>``
    convention a migrated environment's served prefix uses and
    ``internal_schema_version`` is unset or disagrees with N, or when
    ``internal_schema_version`` is set against a ``storage_prefix`` that does
    not follow that convention (bucket root, or a non-standard prefix) — in
    that case there is no digit to check it against.
    """
    match = _PREFIX_SCHEMA_RE.fullmatch(storage_prefix)
    if match is None:
        if internal_schema_version is not None:
            msg = (
                f"omnigraph:internal_schema_version is set to "
                f"{internal_schema_version} but omnigraph:storage_prefix "
                f"{storage_prefix!r} does not follow the fmt<N> convention a "
                "migrated environment's served prefix uses, so there is "
                "nothing to check it against. Unset "
                "internal_schema_version, or set storage_prefix to fmt<N> "
                "if a migration to this schema already landed."
            )
            raise ValueError(msg)
        return
    prefix_version = int(match.group("version"))
    if internal_schema_version is None:
        msg = (
            f"omnigraph:storage_prefix is {storage_prefix!r}, which declares "
            f"internal-schema {prefix_version}, but "
            "omnigraph:internal_schema_version is unset. Set it to "
            f"{prefix_version} to record that this environment's committed "
            "config agrees with itself about which schema it serves — see "
            "docs/omnigraph-storage-format-upgrade-runbook.md."
        )
        raise ValueError(msg)
    if internal_schema_version != prefix_version:
        msg = (
            f"omnigraph:storage_prefix {storage_prefix!r} declares "
            f"internal-schema {prefix_version}, but "
            f"omnigraph:internal_schema_version is {internal_schema_version}. "
            "They must agree — this is exactly the kind of drift that "
            "otherwise reaches the cluster as a crash-looping server rather "
            "than a preview failure."
        )
        raise ValueError(msg)


def storage_uri_for(bucket: str, prefix: str) -> str:
    """Cluster storage root for ``bucket``, optionally under ``prefix``.

    Kept separate from the ``Output.apply`` that calls it so the join is
    testable — an off-by-one slash here silently relocates every graph.
    """
    return f"s3://{bucket}/{prefix}" if prefix else f"s3://{bucket}"
