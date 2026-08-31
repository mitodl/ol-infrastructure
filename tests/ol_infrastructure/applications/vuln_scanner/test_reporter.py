"""Tests for the vuln-scanner reporter: parsing, ASFF construction, and the
Security Hub sync (import + archive) logic.

Covers the specific gaps a PR review on this code called out: both report
formats, an incomplete/empty scan not being mistaken for a clean one,
deterministic finding Ids (and CreatedAt preservation) across re-imports,
BatchImportFindings/BatchUpdateFindings 100-item batching, and partial AWS
failures on both the import and archive paths.
"""

import json
from typing import Any
from unittest.mock import MagicMock

import botocore.session
import pytest
import reporter
from botocore.validate import validate_parameters

# ---------------------------------------------------------------------------
# parse_zap_report
# ---------------------------------------------------------------------------


def _zap_report(*, sites: list[dict[str, Any]]) -> str:
    return json.dumps({"site": sites})


def test_parse_zap_report_extracts_alerts(tmp_path):
    """A well-formed ZAP report parses into RawAlerts with the right fields."""
    report_path = tmp_path / "report.json"
    report_path.write_text(
        _zap_report(
            sites=[
                {
                    "@name": "https://api.rc.learn.mit.edu",
                    "alerts": [
                        {
                            "pluginid": "10020",
                            "name": "Missing Anti-clickjacking Header",
                            "desc": "The response does not include...",
                            "riskcode": 2,
                            "instances": [
                                {"uri": "https://api.rc.learn.mit.edu/api/v1/x"}
                            ],
                        }
                    ],
                }
            ]
        )
    )

    parsed = reporter.parse_zap_report(report_path)

    assert parsed.scan_completed is True
    assert len(parsed.alerts) == 1
    alert = parsed.alerts[0]
    assert alert.rule_id == "10020"
    assert alert.severity == "medium"
    assert alert.location == "https://api.rc.learn.mit.edu/api/v1/x"


def test_parse_zap_report_empty_site_list_marks_scan_incomplete(tmp_path):
    """An empty `site` list means discovery never visited a URL -- not a
    clean scan. Downstream, main() must treat this as a failure, not as
    zero alerts to reconcile against Security Hub.
    """
    report_path = tmp_path / "report.json"
    report_path.write_text(_zap_report(sites=[]))

    parsed = reporter.parse_zap_report(report_path)

    assert parsed.scan_completed is False
    assert parsed.alerts == []


def test_parse_zap_report_null_riskcode_does_not_crash(tmp_path):
    """`{"riskcode": null}` must not reach int(None) -- `.get(..., 0)`'s
    default only applies when the key is absent, not when it's present
    with an explicit None value.
    """
    report_path = tmp_path / "report.json"
    report_path.write_text(
        _zap_report(
            sites=[
                {
                    "@name": "https://api.rc.learn.mit.edu",
                    "alerts": [
                        {
                            "pluginid": "1",
                            "name": "Malformed alert",
                            "riskcode": None,
                            "instances": [{"uri": "https://api.rc.learn.mit.edu/x"}],
                        }
                    ],
                }
            ]
        )
    )

    parsed = reporter.parse_zap_report(report_path)

    assert parsed.alerts[0].severity == "informational"


# ---------------------------------------------------------------------------
# parse_nuclei_report
# ---------------------------------------------------------------------------


def _nuclei_line(**overrides) -> str:
    match = {
        "template-id": "exposed-git-config",
        "info": {"name": "Exposed .git folder", "severity": "medium"},
        "matched-at": "https://api.rc.learn.mit.edu/.git/config",
    }
    match.update(overrides)
    return json.dumps(match)


def test_parse_nuclei_report_extracts_matches(tmp_path):
    """A well-formed Nuclei JSONL line parses into a RawAlert."""
    report_path = tmp_path / "report.jsonl"
    report_path.write_text(_nuclei_line() + "\n")

    parsed = reporter.parse_nuclei_report(report_path)

    assert parsed.scan_completed is True
    assert len(parsed.alerts) == 1
    assert parsed.alerts[0].rule_id == "exposed-git-config"
    assert parsed.alerts[0].severity == "medium"
    assert parsed.alerts[0].cve_ids == ()


def test_parse_nuclei_report_skips_blank_lines(tmp_path):
    """Blank lines in the JSONL output (e.g. trailing newline) are ignored."""
    report_path = tmp_path / "report.jsonl"
    report_path.write_text(f"\n{_nuclei_line()}\n\n")

    parsed = reporter.parse_nuclei_report(report_path)

    assert len(parsed.alerts) == 1


def test_parse_nuclei_report_cve_id_as_bare_string(tmp_path):
    """Nuclei's `info.classification.cve-id` field is a `stringslice.StringSlice`
    in Nuclei's own Go source -- its MarshalJSON passes through whatever
    value the template author set, so real output can be a bare string
    (one CVE) rather than always a list.
    """
    report_path = tmp_path / "report.jsonl"
    report_path.write_text(
        _nuclei_line(
            **{
                "template-id": "CVE-2021-44228-log4shell",
                "info": {
                    "name": "Log4Shell",
                    "severity": "critical",
                    "classification": {"cve-id": "CVE-2021-44228"},
                },
            }
        )
        + "\n"
    )

    parsed = reporter.parse_nuclei_report(report_path)

    assert parsed.alerts[0].cve_ids == ("CVE-2021-44228",)


def test_parse_nuclei_report_cve_id_as_list(tmp_path):
    """The other real shape `cve-id` can take: a JSON array of strings."""
    report_path = tmp_path / "report.jsonl"
    report_path.write_text(
        _nuclei_line(
            info={
                "name": "Multi-CVE template",
                "severity": "high",
                "classification": {"cve-id": ["CVE-2020-1111", "CVE-2020-2222"]},
            }
        )
        + "\n"
    )

    parsed = reporter.parse_nuclei_report(report_path)

    assert parsed.alerts[0].cve_ids == ("CVE-2020-1111", "CVE-2020-2222")


def test_parse_nuclei_report_null_classification_does_not_crash(tmp_path):
    """`{"classification": null}` must not crash `.get("cve-id")` on it."""
    report_path = tmp_path / "report.jsonl"
    report_path.write_text(
        _nuclei_line(info={"name": "x", "severity": "low", "classification": None})
        + "\n"
    )

    parsed = reporter.parse_nuclei_report(report_path)

    assert parsed.alerts[0].cve_ids == ()


def test_parse_nuclei_report_unknown_severity_falls_back_to_informational(tmp_path):
    """An unrecognized severity string doesn't KeyError against SEVERITY_SCALE."""
    report_path = tmp_path / "report.jsonl"
    report_path.write_text(
        _nuclei_line(info={"name": "x", "severity": "not-a-real-severity"}) + "\n"
    )

    parsed = reporter.parse_nuclei_report(report_path)

    assert parsed.alerts[0].severity == "informational"


def _stats_line(**overrides) -> str:
    stats = {
        "duration": "0:00:01",
        "errors": "0",
        "hosts": "1",
        "matched": "0",
        "requests": "1",
        "templates": "1",
        "total": "1",
    }
    stats.update(overrides)
    return json.dumps(stats)


def test_parse_nuclei_report_empty_with_no_stats_path_is_not_completed(tmp_path):
    """No stats file at all (e.g. an older invocation without -stats-json)
    is the conservative case: an empty report alone is never enough to
    trust archival, this is exactly the bug reported against this code by
    a human reviewer (an empty report was previously always treated as a
    genuinely clean, completed scan).
    """
    report_path = tmp_path / "report.jsonl"
    report_path.write_text("")

    parsed = reporter.parse_nuclei_report(report_path, stats_path=None)

    assert parsed.alerts == []
    assert parsed.scan_completed is False


def test_parse_nuclei_report_empty_with_missing_stats_file_is_not_completed(tmp_path):
    """The stats path env var is set but the file was never written --
    treated the same as no signal at all, not assumed clean.
    """
    report_path = tmp_path / "report.jsonl"
    report_path.write_text("")
    stats_path = tmp_path / "stats.jsonl"  # never created

    parsed = reporter.parse_nuclei_report(report_path, stats_path=stats_path)

    assert parsed.scan_completed is False


def test_parse_nuclei_report_empty_with_clean_stats_is_completed(tmp_path):
    """Verified live against the pinned Nuclei image: a target that was
    actually reached, with zero matches, reports `errors: 0` in its
    -stats-json summary -- that's the genuine positive signal that
    distinguishes a real clean scan from one that never reached the target.
    """
    report_path = tmp_path / "report.jsonl"
    report_path.write_text("")
    stats_path = tmp_path / "stats.jsonl"
    stats_path.write_text(_stats_line(errors="0", requests="1") + "\n")

    parsed = reporter.parse_nuclei_report(report_path, stats_path=stats_path)

    assert parsed.alerts == []
    assert parsed.scan_completed is True


def test_parse_nuclei_report_empty_with_errors_is_not_completed(tmp_path):
    """Verified live: an unresolvable target reports `errors > 0` --
    exactly the failure mode (DNS/TLS/routing/WAF/template-load) a human
    reviewer flagged as indistinguishable from a clean scan before this
    check existed.
    """
    report_path = tmp_path / "report.jsonl"
    report_path.write_text("")
    stats_path = tmp_path / "stats.jsonl"
    stats_path.write_text(_stats_line(errors="2", requests="1") + "\n")

    parsed = reporter.parse_nuclei_report(report_path, stats_path=stats_path)

    assert parsed.scan_completed is False


def test_parse_nuclei_report_empty_with_zero_requests_is_not_completed(tmp_path):
    """errors=0 alone isn't sufficient -- a scan that made zero requests
    (e.g. target list filtered to nothing before any request went out)
    reports zero errors trivially but never actually probed anything.
    """
    report_path = tmp_path / "report.jsonl"
    report_path.write_text("")
    stats_path = tmp_path / "stats.jsonl"
    stats_path.write_text(_stats_line(errors="0", requests="0") + "\n")

    parsed = reporter.parse_nuclei_report(report_path, stats_path=stats_path)

    assert parsed.scan_completed is False


def test_parse_nuclei_report_uses_last_stats_line(tmp_path):
    """Nuclei appends one stats line per interval plus a final one at exit
    -- an early tick showing errors could be superseded by a clean final
    tally (or vice versa), so only the last line should be trusted.
    """
    report_path = tmp_path / "report.jsonl"
    report_path.write_text("")
    stats_path = tmp_path / "stats.jsonl"
    stats_path.write_text(
        _stats_line(errors="3", requests="1")
        + "\n"
        + _stats_line(errors="0", requests="5")
        + "\n"
    )

    parsed = reporter.parse_nuclei_report(report_path, stats_path=stats_path)

    assert parsed.scan_completed is True


def test_parse_nuclei_report_tolerates_banner_noise_in_stats_file(tmp_path):
    """Nuclei's -stats-json shares stderr with banner/progress text
    (verified live) -- non-JSON lines in that file must be skipped, not
    crash the parse.
    """
    report_path = tmp_path / "report.jsonl"
    report_path.write_text("")
    stats_path = tmp_path / "stats.jsonl"
    stats_path.write_text(
        "\n"
        "[INF] Templates loaded for current scan: 10730\n"
        + _stats_line(errors="0", requests="1")
        + "\n"
    )

    parsed = reporter.parse_nuclei_report(report_path, stats_path=stats_path)

    assert parsed.scan_completed is True


def test_parse_nuclei_report_matches_present_ignores_bad_stats(tmp_path):
    """A non-empty match list is proof enough on its own -- stats
    shouldn't be consulted (and definitely shouldn't override) when
    matches already confirm the scan reached the target.
    """
    report_path = tmp_path / "report.jsonl"
    report_path.write_text(_nuclei_line() + "\n")
    stats_path = tmp_path / "stats.jsonl"
    stats_path.write_text(_stats_line(errors="5") + "\n")

    parsed = reporter.parse_nuclei_report(report_path, stats_path=stats_path)

    assert parsed.scan_completed is True
    assert len(parsed.alerts) == 1


# ---------------------------------------------------------------------------
# generator_id_for / finding_id
# ---------------------------------------------------------------------------


def test_generator_id_for_is_target_scoped():
    """GeneratorId embeds the target so Security Hub filters can isolate it."""
    assert reporter.generator_id_for("zap", "mitlearn-qa") == (
        "zap-automation-framework/mitlearn-qa"
    )
    assert reporter.generator_id_for("nuclei", "mitlearn-qa") == "nuclei/mitlearn-qa"


def test_finding_id_is_deterministic():
    """Same inputs must hash to the same Id -- re-import updates, not duplicates."""
    args = ("zap-automation-framework/mitlearn-qa", "mitlearn-qa", "10020", "/x")

    assert reporter.finding_id(*args) == reporter.finding_id(*args)


def test_finding_id_differs_across_targets_with_same_rule_and_location():
    """Naive string concatenation could collide once a second target is
    added; hashing a structured (JSON-encoded) tuple must not.
    """
    id_a = reporter.finding_id(
        "zap-automation-framework/target-a", "target-a", "1", "/x"
    )
    id_b = reporter.finding_id(
        "zap-automation-framework/target-b", "target-b", "1", "/x"
    )

    assert id_a != id_b


def test_finding_id_differs_by_rule_or_location():
    """Changing either rule_id or location alone must change the hash."""
    base = ("zap-automation-framework/mitlearn-qa", "mitlearn-qa", "10020", "/x")
    assert reporter.finding_id(*base) != reporter.finding_id(
        "zap-automation-framework/mitlearn-qa", "mitlearn-qa", "99999", "/x"
    )
    assert reporter.finding_id(*base) != reporter.finding_id(
        "zap-automation-framework/mitlearn-qa", "mitlearn-qa", "10020", "/y"
    )


# ---------------------------------------------------------------------------
# build_asff_finding
# ---------------------------------------------------------------------------


def _alert(**overrides) -> reporter.RawAlert:
    fields = {
        "rule_id": "10020",
        "title": "Missing Anti-clickjacking Header",
        "description": "desc",
        "severity": "medium",
        "location": "https://api.rc.learn.mit.edu/x",
        "cve_ids": (),
    }
    fields.update(overrides)
    return reporter.RawAlert(**fields)


def _build_finding(alert, **overrides):
    kwargs = {
        "generator_id": "zap-automation-framework/mitlearn-qa",
        "target_name": "mitlearn-qa",
        "target_url": "https://api.rc.learn.mit.edu",
        "account_id": "610119931565",
        "region": "us-east-1",
        "product_arn": (
            "arn:aws:securityhub:us-east-1:610119931565:product/610119931565/default"
        ),
        "created_at": "2026-08-01T00:00:00Z",
        "updated_at": "2026-08-28T00:00:00Z",
    }
    kwargs.update(overrides)
    return reporter.build_asff_finding(alert, **kwargs)


def test_build_asff_finding_generic_type_when_no_cve():
    """No CVE means the generic Types classifier and no Vulnerabilities field."""
    finding = _build_finding(_alert())

    assert finding["Types"] == [reporter.ASFF_FINDING_TYPE]
    assert finding["FindingProviderFields"]["Types"] == [reporter.ASFF_FINDING_TYPE]
    assert "Vulnerabilities" not in finding


def test_build_asff_finding_cve_type_and_vulnerabilities_when_cve_present():
    """A CVE-tagged alert promotes Types and populates Vulnerabilities."""
    finding = _build_finding(_alert(cve_ids=("CVE-2021-44228",)))

    assert finding["Types"] == [reporter.ASFF_CVE_FINDING_TYPE]
    assert finding["FindingProviderFields"]["Types"] == [reporter.ASFF_CVE_FINDING_TYPE]
    assert finding["Vulnerabilities"] == [{"Id": "CVE-2021-44228"}]


def test_build_asff_finding_preserves_created_at_distinct_from_updated_at():
    """CreatedAt must reflect the finding's original import, not "now" on
    every re-import -- nothing protects it from provider overwrites the way
    Severity/Types are protected once BatchUpdateFindings has touched them.
    """
    finding = _build_finding(
        _alert(), created_at="2026-06-01T00:00:00Z", updated_at="2026-08-28T00:00:00Z"
    )

    assert finding["CreatedAt"] == "2026-06-01T00:00:00Z"
    assert finding["UpdatedAt"] == "2026-08-28T00:00:00Z"


def test_build_asff_finding_severity_under_finding_provider_fields():
    """AWS's own BatchImportFindings docs call setting Severity/Types under
    FindingProviderFields (rather than only top-level) "the preferred
    option" for finding providers -- it's what lets Security Hub protect a
    customer's manual severity override from being clobbered by our next
    weekly re-import.

    FindingProviderFields.Severity is a narrower shape than top-level
    Severity -- confirmed via botocore's own BatchImportFindings service
    model, and the hard way, via a live ParamValidationError against real
    Security Hub: only Label/Original are valid members here, Normalized
    is top-level-only and gets rejected if included.
    """
    finding = _build_finding(_alert(severity="critical"))

    assert finding["FindingProviderFields"]["Severity"] == {
        "Label": "CRITICAL",
        "Original": "critical",
    }
    assert finding["Severity"] == {"Label": "CRITICAL", "Normalized": 95}


def test_build_asff_finding_validates_against_real_batch_import_shape():
    """Regression guard for the exact bug a live run against real Security
    Hub caught (FindingProviderFields.Severity.Normalized -- not a valid
    member of that narrower shape, only of top-level Severity): validates
    the constructed finding against botocore's actual BatchImportFindings
    service model, offline and without AWS credentials. A MagicMock-based
    client (as used elsewhere in this file) doesn't enforce real parameter
    shapes and would not have caught this.
    """
    findings = [
        _build_finding(_alert(severity="critical")),
        _build_finding(_alert(severity="low", cve_ids=("CVE-2020-1111",))),
    ]
    session = botocore.session.get_session()
    op_model = session.get_service_model("securityhub").operation_model(
        "BatchImportFindings"
    )
    validate_parameters({"Findings": findings}, op_model.input_shape)


# ---------------------------------------------------------------------------
# import_findings
# ---------------------------------------------------------------------------


def _finding(id_: str) -> dict[str, Any]:
    return _build_finding(_alert(rule_id=id_, location=f"/{id_}"))


def test_import_findings_batches_at_100():
    """BatchImportFindings caps at 100 findings per call -- chunk accordingly."""
    client = MagicMock()
    client.batch_import_findings.return_value = {"FailedCount": 0}
    findings = [_finding(str(i)) for i in range(150)]

    reporter.import_findings(client, findings)

    assert client.batch_import_findings.call_count == 2
    first_batch = client.batch_import_findings.call_args_list[0]
    second_batch = client.batch_import_findings.call_args_list[1]
    assert len(first_batch.kwargs["Findings"]) == 100
    assert len(second_batch.kwargs["Findings"]) == 50


def test_import_findings_raises_on_partial_failure():
    """A non-zero FailedCount must raise, not just log and continue."""
    client = MagicMock()
    client.batch_import_findings.return_value = {
        "FailedCount": 1,
        "FailedFindings": [{"Id": "abc", "ErrorMessage": "boom"}],
    }

    with pytest.raises(RuntimeError, match="1 findings failed to import"):
        reporter.import_findings(client, [_finding("1")])


def test_import_findings_noop_on_empty_list():
    """Zero findings to import must not call the API at all."""
    client = MagicMock()

    reporter.import_findings(client, [])

    client.batch_import_findings.assert_not_called()


# ---------------------------------------------------------------------------
# fetch_existing_findings / archive_stale_findings
# ---------------------------------------------------------------------------


def _paginated_client(pages: list[list[dict[str, Any]]]) -> MagicMock:
    client = MagicMock()
    paginator = MagicMock()
    paginator.paginate.return_value = [{"Findings": page} for page in pages]
    client.get_paginator.return_value = paginator
    return client


def _existing_finding(id_: str, **overrides) -> dict[str, Any]:
    finding = {
        "Id": id_,
        "ProductArn": (
            "arn:aws:securityhub:us-east-1:610119931565:product/610119931565/default"
        ),
        "GeneratorId": "zap-automation-framework/mitlearn-qa",
        "AwsAccountId": "610119931565",
        "SchemaVersion": "2018-10-08",
        "Types": ["Software and Configuration Checks/Vulnerabilities"],
        "CreatedAt": "2026-01-01T00:00:00Z",
        "UpdatedAt": "2026-01-01T00:00:00Z",
        "Severity": {"Label": "LOW", "Normalized": 30},
        "Title": "t",
        "Description": "d",
        "Resources": [{"Type": "Other", "Id": "x"}],
        "RecordState": "ACTIVE",
    }
    finding.update(overrides)
    return finding


def test_fetch_existing_findings_returns_id_to_full_finding_map():
    """Paginated GetFindings results flatten into one {Id: full finding} map."""
    client = _paginated_client(
        [
            [
                _existing_finding("a"),
                _existing_finding("b", CreatedAt="2026-02-01T00:00:00Z"),
            ]
        ]
    )

    existing = reporter.fetch_existing_findings(
        client, product_arn="arn:...:default", generator_id="zap-automation-framework/x"
    )

    assert set(existing) == {"a", "b"}
    assert existing["a"]["CreatedAt"] == "2026-01-01T00:00:00Z"
    assert existing["b"]["CreatedAt"] == "2026-02-01T00:00:00Z"


def test_archive_stale_findings_uses_batch_import_not_batch_update():
    """RecordState can only be set via BatchImportFindings (confirmed against
    boto3's own service model: BatchUpdateFindings has no RecordState input
    member at all, and passing one raises ParamValidationError client-side,
    before any network call). archive_stale_findings must never call
    batch_update_findings for this.
    """
    client = MagicMock()
    client.batch_import_findings.return_value = {"FailedCount": 0}
    existing = {"a": _existing_finding("a")}

    reporter.archive_stale_findings(
        client, existing=existing, current_ids=set(), updated_at="2026-08-28T00:00:00Z"
    )

    client.batch_update_findings.assert_not_called()
    client.batch_import_findings.assert_called_once()
    sent = client.batch_import_findings.call_args.kwargs["Findings"][0]
    assert sent["RecordState"] == "ARCHIVED"
    assert sent["UpdatedAt"] == "2026-08-28T00:00:00Z"
    assert sent["Id"] == "a"


def test_archive_stale_findings_archives_only_missing_ids():
    """Only Ids absent from the current run get archived, not the whole set."""
    client = MagicMock()
    client.batch_import_findings.return_value = {"FailedCount": 0}
    existing = {
        "a": _existing_finding("a"),
        "b": _existing_finding("b"),
        "c": _existing_finding("c"),
    }

    reporter.archive_stale_findings(
        client, existing=existing, current_ids={"b"}, updated_at="2026-08-28T00:00:00Z"
    )

    archived_ids = {
        f["Id"] for f in client.batch_import_findings.call_args.kwargs["Findings"]
    }
    assert archived_ids == {"a", "c"}


def test_archive_stale_findings_noop_when_nothing_stale():
    """Nothing stale means no API call at all -- not a call with an empty list."""
    client = MagicMock()
    existing = {"a": _existing_finding("a")}

    reporter.archive_stale_findings(
        client, existing=existing, current_ids={"a"}, updated_at="2026-08-28T00:00:00Z"
    )

    client.batch_import_findings.assert_not_called()


def test_archive_stale_findings_batches_at_100():
    """BatchImportFindings caps at 100 findings per call -- chunk accordingly."""
    client = MagicMock()
    client.batch_import_findings.return_value = {"FailedCount": 0}
    existing = {str(i): _existing_finding(str(i)) for i in range(150)}

    reporter.archive_stale_findings(
        client, existing=existing, current_ids=set(), updated_at="2026-08-28T00:00:00Z"
    )

    assert client.batch_import_findings.call_count == 2


def test_archive_stale_findings_raises_on_partial_failure():
    """BatchImportFindings' FailedCount/FailedFindings must raise -- ignoring
    it would let the job log "archived" and exit 0 while some findings
    silently stayed ACTIVE.
    """
    client = MagicMock()
    client.batch_import_findings.return_value = {
        "FailedCount": 1,
        "FailedFindings": [{"Id": "a", "ErrorMessage": "boom"}],
    }
    existing = {"a": _existing_finding("a")}

    with pytest.raises(RuntimeError, match="1 findings failed to import"):
        reporter.archive_stale_findings(
            client,
            existing=existing,
            current_ids=set(),
            updated_at="2026-08-28T00:00:00Z",
        )
