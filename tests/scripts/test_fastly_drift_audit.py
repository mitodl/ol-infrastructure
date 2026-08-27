"""Tests for bin/fastly-drift-audit.

The three trap cases below each produced a permanently firing false alarm in a
prototype of this audit. A nightly check that is not silent when the estate is
healthy gets muted within a week, so each trap is pinned here.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPT_PATH = Path(__file__).resolve().parents[2] / "bin" / "fastly-drift-audit"


def load_audit_module():
    """Load the audit CLI from bin/.

    An explicit ``SourceFileLoader`` is required: the script has no ``.py``
    suffix, so import machinery cannot infer a loader for it.
    """
    name = "test_fastly_drift_audit_script"
    spec = importlib.util.spec_from_file_location(
        name,
        SCRIPT_PATH,
        loader=importlib.machinery.SourceFileLoader(name, str(SCRIPT_PATH)),
    )
    if spec is None or spec.loader is None:
        msg = f"Unable to load module from {SCRIPT_PATH}"
        raise RuntimeError(msg)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def audit():
    """Return the loaded audit module."""
    return load_audit_module()


def service(audit, **collections):
    """Build a DeclaredService whose collections are plain name sets."""
    return audit.DeclaredService(
        project="ol-application-mitxonline",
        stack="Production",
        service_id="54YRiE18QJhCPQRlVmFHFm",
        state_active_version=207,
        collections={
            key: collections.get(key, audit.DeclaredCollection(frozenset()))
            for key in audit.AUDITED_COLLECTIONS
        },
    )


def kinds(findings):
    """Reduce findings to the tuples the assertions care about."""
    return {(f.level, f.kind, f.collection) for f in findings}


# --- The set-diff direction -------------------------------------------------


def test_declared_but_absent_is_the_alert(audit):
    """hq#12449 exactly: state claims a snippet Fastly does not have."""
    snippet = "Redirect course and program pages to MIT Learn"
    findings = audit.compare(
        service(audit, snippets=audit.DeclaredCollection(frozenset({snippet}))),
        207,
        dict.fromkeys(audit.AUDITED_COLLECTIONS, frozenset())
        | {"snippets": frozenset()},
    )
    assert kinds(findings) == {(audit.ALERT, "declared-but-absent", "snippets")}
    assert findings[0].names == (snippet,)


def test_live_but_undeclared_does_not_alert(audit):
    """An object added by hand in the Fastly UI is reported, not failed.

    Failing on this direction too would make every manual UI edit page
    somebody overnight, which is how a check gets muted.
    """
    findings = audit.compare(
        service(audit),
        207,
        dict.fromkeys(audit.AUDITED_COLLECTIONS, frozenset())
        | {"snippets": frozenset({"added in the UI"})},
    )
    assert kinds(findings) == {(audit.INFO, "live-but-undeclared", "snippets")}


def test_matching_names_are_silent(audit):
    """The healthy case emits nothing at all."""
    names = frozenset({"one", "two"})
    assert (
        audit.compare(
            service(audit, snippets=audit.DeclaredCollection(names)),
            207,
            dict.fromkeys(audit.AUDITED_COLLECTIONS, frozenset()) | {"snippets": names},
        )
        == ()
    )


def test_version_mismatch_is_informational_only(audit):
    """State and live disagreeing on the active version is not the failure.

    In hq#12449 both sides said 207; it was the names that differed. A
    mismatch alone is an ordinary consequence of a manual UI edit.
    """
    findings = audit.compare(
        service(audit), 209, dict.fromkeys(audit.AUDITED_COLLECTIONS, frozenset())
    )
    assert kinds(findings) == {(audit.INFO, "active-version-mismatch", "")}


def test_no_active_version_skips_rather_than_alerting(audit):
    """A service with nothing serving traffic has nothing to compare against."""
    findings = audit.compare(service(audit), None, {})
    assert kinds(findings) == {(audit.SKIP, "no-active-version", "")}


# --- Trap 1: secret-wrapped collections -------------------------------------


def test_secret_wrapped_collection_is_unauditable_not_empty(audit):
    """`backends` and `loggingHttps` are sometimes stored secret-wrapped.

    Iterating the sigil dict yields zero names, so every live backend would
    read as `live-but-undeclared` on every service, forever.
    """
    collection = audit.read_collection(
        # Detection keys off the sigil, so the value half of Pulumi's sentinel
        # pair is irrelevant here and is left out rather than tripping secret
        # scanners with a well-known constant.
        {audit.SECRET_SIGIL: "<sentinel>", "ciphertext": "v1:..."}
    )
    assert collection.auditable is False
    assert collection.names == frozenset()

    findings = audit.compare(
        service(audit, backends=collection),
        207,
        dict.fromkeys(audit.AUDITED_COLLECTIONS, frozenset()),
    )
    assert kinds(findings) == {(audit.INFO, "unauditable", "backends")}


def test_unauditable_and_empty_are_distinguishable(audit):
    """An absent collection is genuinely empty; an unreadable one is not.

    Collapsing the two would make "we could not check this" indistinguishable
    from "there is nothing to check".
    """
    assert audit.read_collection(None) == audit.DeclaredCollection(frozenset())
    assert audit.read_collection([]).auditable is True
    assert audit.read_collection("unexpected").auditable is False


# --- Trap 3: non-dict collection members ------------------------------------


def test_plain_string_members_are_unauditable(audit):
    """`TlsSubscription` also has a `domains` output, of plain strings.

    Type filtering keeps those resources out, but a collection whose members
    carry no name must not silently shrink the declared set -- that errs
    toward reporting clean, the wrong direction for a detector.
    """
    collection = audit.read_collection(["courses.mitxonline.mit.edu"])
    assert collection.auditable is False
    assert collection.names == frozenset()

    mixed = audit.read_collection([{"name": "real"}, {"no_name": True}])
    assert mixed.auditable is False


def test_well_formed_collection_yields_its_names(audit):
    """The ordinary case: a list of dicts reduces to its names."""
    assert audit.read_collection(
        [{"name": "a", "content": "..."}, {"name": "b", "content": "..."}]
    ) == audit.DeclaredCollection(frozenset({"a", "b"}))


# --- Trap 2: scope, and checkpoint parsing ----------------------------------


def test_scope_comes_from_declared_projects(audit, tmp_path):
    """Scope is parsed from src/**/Pulumi.yaml, never from an S3 listing.

    The bucket retains checkpoints for deleted projects that point at the same
    live service IDs as the current stacks, hundreds of versions stale.
    """
    (tmp_path / "src" / "applications" / "mitxonline").mkdir(parents=True)
    (tmp_path / "src" / "applications" / "mitxonline" / "Pulumi.yaml").write_text(
        "name: ol-application-mitxonline\nruntime: python\n"
    )
    (tmp_path / "src" / "nameless").mkdir(parents=True)
    (tmp_path / "src" / "nameless" / "Pulumi.yaml").write_text("runtime: python\n")

    assert audit.declared_projects(tmp_path) == {"ol-application-mitxonline"}


def test_empty_scope_raises_rather_than_reporting_clean(audit, tmp_path):
    """Auditing nothing must not look like a healthy estate."""
    (tmp_path / "src").mkdir()
    with pytest.raises(RuntimeError, match="No Pulumi projects"):
        audit.declared_projects(tmp_path)


def test_declared_services_reads_service_vcl_resources(audit):
    """Only ServiceVcl resources are picked out of a stack checkpoint."""
    checkpoint = {
        "checkpoint": {
            "latest": {
                "resources": [
                    {
                        "type": "pulumi:pulumi:Stack",
                        "outputs": {"id": "not-a-service"},
                    },
                    {
                        "type": audit.SERVICE_VCL_TYPE,
                        "outputs": {
                            "id": "54YRiE18QJhCPQRlVmFHFm",
                            "activeVersion": 208,
                            "snippets": [{"name": "redirect"}],
                        },
                    },
                ]
            }
        }
    }
    (found,) = audit.declared_services("proj", "Production", checkpoint)
    assert found.service_id == "54YRiE18QJhCPQRlVmFHFm"
    assert found.state_active_version == 208
    assert found.collections["snippets"].names == frozenset({"redirect"})
    # Collections the service does not declare are empty, not unauditable.
    assert found.collections["dictionaries"] == audit.DeclaredCollection(frozenset())


def test_resources_pending_deletion_are_not_the_authority(audit):
    """A `delete: true` resource is not what should be live."""
    checkpoint = {
        "checkpoint": {
            "latest": {
                "resources": [
                    {
                        "type": audit.SERVICE_VCL_TYPE,
                        "delete": True,
                        "outputs": {"id": "doomed"},
                    }
                ]
            }
        }
    }
    assert audit.declared_services("proj", "Production", checkpoint) == []


def test_empty_checkpoint_is_not_an_error(audit):
    """A stack with no deployment yet has no services, and that is fine."""
    assert audit.declared_services("proj", "Production", {}) == []
    assert audit.declared_services("proj", "Production", {"checkpoint": {}}) == []


# --- Live-side parsing ------------------------------------------------------


def test_serving_version_picks_the_active_one(audit):
    """The comparison target is the serving version, not the highest one."""
    details = {
        "versions": [
            {"number": 207, "active": False},
            {"number": 208, "active": True},
            {"number": 209, "active": False},
        ]
    }
    assert audit.serving_version(details) == 208


def test_serving_version_is_none_when_nothing_is_active(audit):
    """A service with no active version reports None rather than guessing."""
    assert audit.serving_version({"versions": [{"number": 1, "active": False}]}) is None
    assert audit.serving_version({}) is None


def test_live_names_requires_a_list(audit):
    """A Fastly error body must fail loudly, not read as an empty collection."""
    assert audit.live_names([{"name": "a"}, {"name": "b"}]) == frozenset({"a", "b"})
    with pytest.raises(TypeError, match="Expected a list"):
        audit.live_names({"msg": "Record not found"})


# --- Coverage of the collection list itself ---------------------------------


def test_every_name_bearing_collection_in_use_is_audited(audit):
    """An unaudited collection reports nothing, which reads as clean.

    The first draft audited seven collections and silently ignored ~100 live
    objects across these four. Coverage is the detector's blind spot, so the
    set is pinned rather than left to drift as collections are adopted.
    """
    assert set(audit.AUDITED_COLLECTIONS) == {
        "snippets",
        "conditions",
        "headers",
        "backends",
        "domains",
        "requestSettings",
        "dictionaries",
        "cacheSettings",
        "gzips",
        "responseObjects",
        "loggingHttps",
    }


def test_endpoint_paths_are_the_fastly_spellings(audit):
    """State keys and API path segments differ; a wrong one 404s at runtime."""
    assert audit.AUDITED_COLLECTIONS["requestSettings"] == "request_settings"
    assert audit.AUDITED_COLLECTIONS["responseObjects"] == "response_object"
    assert audit.AUDITED_COLLECTIONS["cacheSettings"] == "cache_settings"
    assert audit.AUDITED_COLLECTIONS["gzips"] == "gzip"
    assert audit.AUDITED_COLLECTIONS["loggingHttps"] == "logging/https"


def test_drift_and_error_exit_codes_are_distinct(audit):
    """Concourse calls every nonzero exit `failed` and fires one hook.

    If a crash exited 1 like real drift does, an S3 or Fastly outage would
    announce confirmed drift every time.
    """
    codes = {audit.EXIT_CLEAN, audit.EXIT_DRIFT, audit.EXIT_USAGE, audit.EXIT_ERROR}
    assert len(codes) == 4
    assert audit.EXIT_CLEAN == 0
