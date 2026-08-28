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

import pytest
import reporter

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
    """
    finding = _build_finding(_alert(severity="critical"))

    assert finding["FindingProviderFields"]["Severity"] == {
        "Label": "CRITICAL",
        "Normalized": 95,
        "Original": "critical",
    }
    assert finding["Severity"] == {"Label": "CRITICAL", "Normalized": 95}


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


def test_fetch_existing_findings_returns_id_to_created_at_map():
    """Paginated GetFindings results flatten into one {Id: CreatedAt} map."""
    client = _paginated_client(
        [
            [
                {"Id": "a", "CreatedAt": "2026-01-01T00:00:00Z"},
                {"Id": "b", "CreatedAt": "2026-02-01T00:00:00Z"},
            ]
        ]
    )

    existing = reporter.fetch_existing_findings(
        client, product_arn="arn:...:default", generator_id="zap-automation-framework/x"
    )

    assert existing == {
        "a": "2026-01-01T00:00:00Z",
        "b": "2026-02-01T00:00:00Z",
    }


def test_archive_stale_findings_archives_only_missing_ids():
    """Only Ids absent from the current run get archived, not the whole set."""
    client = MagicMock()
    client.batch_update_findings.return_value = {"UnprocessedFindings": []}

    reporter.archive_stale_findings(
        client,
        product_arn="arn:aws:securityhub:us-east-1:610119931565:product/610119931565/default",
        generator_id="zap-automation-framework/mitlearn-qa",
        previous_ids={"a", "b", "c"},
        current_ids={"b"},
    )

    client.batch_update_findings.assert_called_once()
    archived_ids = {
        identifier["Id"]
        for identifier in client.batch_update_findings.call_args.kwargs[
            "FindingIdentifiers"
        ]
    }
    assert archived_ids == {"a", "c"}
    assert client.batch_update_findings.call_args.kwargs["RecordState"] == "ARCHIVED"


def test_archive_stale_findings_noop_when_nothing_stale():
    """Nothing stale means no API call at all -- not a call with an empty list."""
    client = MagicMock()

    reporter.archive_stale_findings(
        client,
        product_arn="arn:...:default",
        generator_id="zap-automation-framework/mitlearn-qa",
        previous_ids={"a"},
        current_ids={"a"},
    )

    client.batch_update_findings.assert_not_called()


def test_archive_stale_findings_batches_at_100():
    """BatchUpdateFindings caps at 100 identifiers per call -- chunk accordingly."""
    client = MagicMock()
    client.batch_update_findings.return_value = {"UnprocessedFindings": []}
    previous_ids = {str(i) for i in range(150)}

    reporter.archive_stale_findings(
        client,
        product_arn="arn:...:default",
        generator_id="zap-automation-framework/mitlearn-qa",
        previous_ids=previous_ids,
        current_ids=set(),
    )

    assert client.batch_update_findings.call_count == 2


def test_archive_stale_findings_raises_on_unprocessed():
    """BatchUpdateFindings reports per-finding failures in
    UnprocessedFindings without raising an SDK exception -- ignoring the
    response would let the job log "archived" and exit 0 while some
    findings silently stayed ACTIVE.
    """
    client = MagicMock()
    client.batch_update_findings.return_value = {
        "UnprocessedFindings": [
            {
                "FindingIdentifier": {"Id": "a", "ProductArn": "arn:...:default"},
                "ErrorMessage": "boom",
            }
        ]
    }

    with pytest.raises(RuntimeError, match="1 findings failed to archive"):
        reporter.archive_stale_findings(
            client,
            product_arn="arn:...:default",
            generator_id="zap-automation-framework/mitlearn-qa",
            previous_ids={"a"},
            current_ids=set(),
        )
