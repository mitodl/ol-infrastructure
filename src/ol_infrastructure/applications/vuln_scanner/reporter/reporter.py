"""Reporter step for the vuln-scanner CronJobs.

Runs as the main container after ZAP or Nuclei (the initContainer) writes its
raw report to a shared volume. Responsibilities, in order:

1. Upload the raw report to S3, under ``<tool>/<target>/<date>/``.
2. Convert each alert/match into an AWS Security Finding Format (ASFF)
   finding and import it into Security Hub via ``BatchImportFindings``.
3. Archive (via ``BatchUpdateFindings``) any previously-imported finding for
   this (tool, target) pair that no longer appears in the current run --
   ASFF findings never auto-resolve, so without this step a fixed issue
   would sit ``RecordState: ACTIVE`` in Security Hub forever.

No official ASFF template exists for ZAP or Nuclei output (unlike Trivy),
hence this custom mapping. See the "ASFF finding lifecycle" section of the
vuln-scanner plan doc for why the two correctness properties below
(deterministic Id, structured hash input) are not optional details.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import boto3

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("vuln-scanner-reporter")

# ASFF Types namespace for generic web-app vulnerability findings. Not every
# ZAP/Nuclei alert is a CVE, so the more specific ".../CVE" classifier isn't
# always accurate -- this is the closest correct generic category.
ASFF_FINDING_TYPE = "Software and Configuration Checks/Vulnerabilities"
# Used instead of ASFF_FINDING_TYPE when a Nuclei match's template
# references a known CVE (RawAlert.cve_ids non-empty) -- surfaces those
# findings in Security Hub's CVE-specific filters/views alongside
# Inspector's CVE findings, same category ZAP alerts can't claim since
# they're CWE/category-based, not tied to a specific CVE.
ASFF_CVE_FINDING_TYPE = "Software and Configuration Checks/Vulnerabilities/CVE"
ASFF_SCHEMA_VERSION = "2018-10-08"

# (label, normalized 0-100 score) -- mid-bucket representative values, not
# meant to be finely tuned. ZAP's riskcode (0-3) and Nuclei's info.severity
# both collapse onto this same five-point scale.
SEVERITY_SCALE: dict[str, tuple[str, int]] = {
    "informational": ("INFORMATIONAL", 0),
    "low": ("LOW", 30),
    "medium": ("MEDIUM", 50),
    "high": ("HIGH", 80),
    "critical": ("CRITICAL", 95),
}

ZAP_RISKCODE_TO_SEVERITY = {
    0: "informational",
    1: "low",
    2: "medium",
    3: "high",
}


@dataclass(frozen=True)
class RawAlert:
    """A single tool-agnostic alert/match, before ASFF conversion."""

    rule_id: str
    title: str
    description: str
    severity: str  # one of SEVERITY_SCALE's keys
    location: str  # URL/path the alert was raised against
    # Populated for Nuclei matches whose template references a known CVE
    # (ZAP's alerts are CWE/category-based, not CVE-based, so always empty
    # there). Drives the more specific ASFF CVE finding type -- see
    # build_asff_finding.
    cve_ids: tuple[str, ...] = ()


def _env(name: str, *, required: bool = True, default: str = "") -> str:
    value = os.environ.get(name, default)
    if required and not value:
        msg = f"Required environment variable {name} is not set"
        raise SystemExit(msg)
    return value


@dataclass(frozen=True)
class ParsedReport:
    """Parsed alerts plus whether the scan actually ran against the target.

    `scan_completed=False` means: don't trust a zero-alert result enough to
    archive previously-active findings. Without this distinction, a scan
    that silently fails before reaching the target (bad config, target
    down, network blip) but still produces a well-formed empty report would
    be indistinguishable from "everything got fixed" -- and the
    archive-diff step in `archive_stale_findings` would wipe out every
    previously-tracked finding for that target on the strength of nothing
    having run at all.
    """

    alerts: list[RawAlert]
    scan_completed: bool


def parse_zap_report(report_path: Path) -> ParsedReport:
    """Parse a ZAP Automation Framework JSON report (the `report` job's
    `traditional-json` template output).

    Shape: {"site": [{"alerts": [{"pluginid", "name", "desc", "riskcode",
    "instances": [{"uri": ...}, ...]}, ...]}, ...]}
    """
    data = json.loads(report_path.read_text())
    sites = data.get("site", [])
    alerts: list[RawAlert] = []
    for site in sites:
        for alert in site.get("alerts", []):
            # `.get(..., 0)`'s default only applies when the key is
            # absent -- an explicit `"riskcode": null` would still reach
            # int(None) and crash the whole run. `or 0` catches both.
            severity = ZAP_RISKCODE_TO_SEVERITY.get(
                int(alert.get("riskcode") or 0), "informational"
            )
            instances = alert.get("instances") or [{}]
            for instance in instances:
                location = instance.get("uri") or site.get("@name", "unknown")
                alerts.append(
                    RawAlert(
                        rule_id=str(alert.get("pluginid", "unknown")),
                        title=alert.get("name", "Unnamed ZAP alert"),
                        description=alert.get("desc", ""),
                        severity=severity,
                        location=location,
                    )
                )
    # An empty `site` list means the openapi/spider discovery job never
    # actually visited any URL -- a real signal already free in this same
    # report, distinct from "visited URLs and found nothing to report."
    return ParsedReport(alerts=alerts, scan_completed=bool(sites))


def _nuclei_cve_ids(info: dict[str, Any]) -> tuple[str, ...]:
    """Extract CVE IDs from a Nuclei match's `info.classification.cve-id`.

    Verified against Nuclei's own source (pkg/model/model.go's
    `Classification.CVEID`, typed `stringslice.StringSlice`): its
    `MarshalJSON` passes through a bare `interface{}`
    (pkg/model/types/stringslice/stringslice.go), so in real scan output
    this field can be either a plain string (a template with one CVE) or a
    JSON array of strings (a template referencing several) -- not just one
    shape to assume.
    """
    cve_id = (info.get("classification") or {}).get("cve-id")
    if isinstance(cve_id, str):
        return (cve_id,)
    if isinstance(cve_id, list):
        return tuple(str(c) for c in cve_id)
    return ()


def parse_nuclei_report(report_path: Path) -> ParsedReport:
    """Parse Nuclei's `-jsonl` output: one JSON object per matched line."""
    alerts: list[RawAlert] = []
    for raw_line in report_path.read_text().splitlines():
        stripped_line = raw_line.strip()
        if not stripped_line:
            continue
        match = json.loads(stripped_line)
        info = match.get("info", {})
        severity = str(info.get("severity", "informational")).lower()
        if severity not in SEVERITY_SCALE:
            severity = "informational"
        alerts.append(
            RawAlert(
                rule_id=str(match.get("template-id", "unknown")),
                title=info.get("name", "Unnamed Nuclei finding"),
                description=info.get("description", "") or info.get("name", ""),
                severity=severity,
                location=match.get("matched-at") or match.get("host", "unknown"),
                cve_ids=_nuclei_cve_ids(info),
            )
        )
    # Known gap, not silently ignored: unlike ZAP's `site` list, Nuclei's
    # `-jsonl` output has no equivalent "did it actually probe the target"
    # signal when zero matches come back -- a genuinely clean scan and a
    # scan that never reached the target both produce an empty file.
    # scan_completed is left True (today's actual behavior, unchanged) until
    # a reliable positive signal is added (e.g. parsing Nuclei's own
    # end-of-run stats output, once CLI flags are verified against the
    # pinned image -- see the flag-verification note in __main__.py).
    return ParsedReport(alerts=alerts, scan_completed=True)


PARSERS = {
    "zap": parse_zap_report,
    "nuclei": parse_nuclei_report,
}


def generator_id_for(tool: str, target_name: str) -> str:
    """Build a target-scoped GeneratorId, e.g. "nuclei/mitlearn-qa"."""
    # Target-scoped on purpose: Security Hub's GetFindings/BatchUpdateFindings
    # filters don't support querying the nested Resources[].Details
    # map findings carry (TargetName), and each finding's Resources[0].Id is
    # the alert's specific location URL, not the bare target URL -- so
    # filtering by a plain "zap-automation-framework" GeneratorId plus a
    # ResourceId=target_url filter would silently match nothing. Folding the
    # target into GeneratorId gives the archive-diff step (below) a filter
    # that actually isolates this target's findings from any other target
    # sharing the same tool.
    base = {"zap": "zap-automation-framework", "nuclei": "nuclei"}[tool]
    return f"{base}/{target_name}"


def finding_id(generator_id: str, target_name: str, rule_id: str, location: str) -> str:
    """Deterministic finding Id -- re-importing the same Id updates the
    existing finding in place; a fresh Id every run creates a duplicate.

    Hashes a structured (JSON-encoded) tuple rather than concatenating raw
    strings, since `vuln_scanner:targets` is designed to grow beyond one
    target and naive concatenation risks two targets' (rule_id, location)
    pairs colliding into the same hash.
    """
    payload = json.dumps([generator_id, target_name, rule_id, location], sort_keys=True)
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return f"{generator_id}/{digest}"


def build_asff_finding(
    alert: RawAlert,
    *,
    generator_id: str,
    target_name: str,
    target_url: str,
    account_id: str,
    region: str,
    product_arn: str,
    created_at: str,
    updated_at: str,
) -> dict[str, Any]:
    """Convert one RawAlert into a Security Hub ASFF finding dict.

    `created_at` must be the finding's *original* CreatedAt (looked up from
    the existing finding by the caller when this Id has been imported
    before, "now" only for a genuinely new finding) -- unlike `updated_at`,
    which is always "now". Per AWS's own BatchImportFindings docs, nothing
    protects CreatedAt from being overwritten on every re-import the way
    Severity/Types are protected once a customer has touched them via
    BatchUpdateFindings; recomputing it fresh every run would permanently
    erase how long a finding has actually been open, a signal security
    teams rely on for triage/SLA purposes.
    """
    label, normalized = SEVERITY_SCALE[alert.severity]
    finding_type = ASFF_CVE_FINDING_TYPE if alert.cve_ids else ASFF_FINDING_TYPE
    finding: dict[str, Any] = {
        "SchemaVersion": ASFF_SCHEMA_VERSION,
        "Id": finding_id(generator_id, target_name, alert.rule_id, alert.location),
        "ProductArn": product_arn,
        "GeneratorId": generator_id,
        "AwsAccountId": account_id,
        "CreatedAt": created_at,
        "UpdatedAt": updated_at,
        "Title": f"[{target_name}] {alert.title}"[:256],
        "Description": (alert.description or alert.title)[:1024],
        # Severity/Types are set under FindingProviderFields, not (only)
        # top-level -- AWS's docs on BatchImportFindings call this "the
        # preferred option" for finding providers precisely because it's
        # what lets Security Hub tell a provider's routine re-import apart
        # from a customer's manual override via BatchUpdateFindings, so a
        # weekly re-scan can't silently clobber an analyst's triage
        # decision. Top-level copies are included too since AWS populates
        # them from FindingProviderFields on first creation anyway (and
        # leaves them alone afterward if a customer already touched them),
        # so this just makes a brand-new finding immediately correct
        # without waiting on that replication step.
        "FindingProviderFields": {
            "Severity": {
                "Label": label,
                "Normalized": normalized,
                "Original": alert.severity,
            },
            "Types": [finding_type],
        },
        "Types": [finding_type],
        "Severity": {"Label": label, "Normalized": normalized},
        "Resources": [
            {
                "Type": "Other",
                "Id": alert.location or target_url,
                "Region": region,
                "Details": {
                    "Other": {
                        "TargetName": target_name,
                        "TargetUrl": target_url,
                        "RuleId": alert.rule_id,
                    }
                },
            }
        ],
        "RecordState": "ACTIVE",
        "Workflow": {"Status": "NEW"},
    }
    if alert.cve_ids:
        # The actual CVE identifier(s) -- Types/ASFF_CVE_FINDING_TYPE above
        # only classifies the finding as CVE-related, it doesn't surface
        # which CVE. This is the ASFF field the console reads to link out
        # to NVD and to populate Security Hub's dedicated Vulnerabilities
        # view. Omitted entirely (not sent as an empty list) when there's
        # nothing to put here, e.g. every ZAP alert and any Nuclei match
        # whose template isn't CVE-based.
        finding["Vulnerabilities"] = [{"Id": cve_id} for cve_id in alert.cve_ids]
    return finding


def upload_raw_report(
    s3_client: Any,
    *,
    bucket: str,
    tool: str,
    target_name: str,
    report_path: Path,
    scan_date: str,
) -> str:
    """Upload the raw report file to S3 under `<tool>/<target>/<date>/`."""
    key = f"{tool}/{target_name}/{scan_date}/{report_path.name}"
    s3_client.upload_file(str(report_path), bucket, key)
    logger.info("Uploaded raw report to s3://%s/%s", bucket, key)
    return key


def import_findings(securityhub_client: Any, findings: list[dict[str, Any]]) -> None:
    """Import findings into Security Hub via BatchImportFindings, in batches of 100."""
    # BatchImportFindings caps at 100 findings per call.
    for i in range(0, len(findings), 100):
        batch = findings[i : i + 100]
        response = securityhub_client.batch_import_findings(Findings=batch)
        if response.get("FailedCount"):
            for failure in response["FailedFindings"]:
                logger.error(
                    "Failed to import finding %s: %s",
                    failure.get("Id"),
                    failure.get("ErrorMessage"),
                )
            msg = f"{response['FailedCount']} findings failed to import"
            raise RuntimeError(msg)
    logger.info("Imported %d findings into Security Hub", len(findings))


def fetch_existing_findings(
    securityhub_client: Any, *, product_arn: str, generator_id: str
) -> dict[str, str]:
    """Return `{finding Id: CreatedAt}` for this (generator, target)'s
    currently-ACTIVE findings.

    Scoped by GeneratorId alone (already target-specific -- see
    `generator_id_for`), since Security Hub's GetFindings filters can't query
    the nested Resources[].Details.Other.TargetName map each finding carries.
    Shared by `main` (to preserve each existing finding's original CreatedAt
    on re-import) and `archive_stale_findings` (to know what's no longer
    present), so this only queries once per run rather than twice.
    """
    existing: dict[str, str] = {}
    paginator = securityhub_client.get_paginator("get_findings")
    for page in paginator.paginate(
        Filters={
            "ProductArn": [{"Value": product_arn, "Comparison": "EQUALS"}],
            "GeneratorId": [{"Value": generator_id, "Comparison": "EQUALS"}],
            "RecordState": [{"Value": "ACTIVE", "Comparison": "EQUALS"}],
        }
    ):
        for finding in page.get("Findings", []):
            existing[finding["Id"]] = finding["CreatedAt"]
    return existing


def archive_stale_findings(
    securityhub_client: Any,
    *,
    product_arn: str,
    generator_id: str,
    previous_ids: set[str],
    current_ids: set[str],
) -> None:
    """Archive any previously-active finding for this (generator, target)
    that isn't present in the current run -- ASFF findings never
    auto-resolve, so this is the only thing that makes a fixed issue clear.
    """
    stale_ids = previous_ids - current_ids
    if not stale_ids:
        logger.info("No stale findings to archive for %s", generator_id)
        return

    identifiers = [{"Id": id_, "ProductArn": product_arn} for id_ in stale_ids]
    # BatchUpdateFindings caps at 100 identifiers per call.
    for i in range(0, len(identifiers), 100):
        response = securityhub_client.batch_update_findings(
            FindingIdentifiers=identifiers[i : i + 100],
            RecordState="ARCHIVED",
        )
        # Per-finding failures land in UnprocessedFindings, not an SDK
        # exception -- ignoring this would let the job log "archived" and
        # exit 0 while some findings silently stayed ACTIVE, the same
        # partial-failure class import_findings already guards against.
        unprocessed = response.get("UnprocessedFindings", [])
        if unprocessed:
            for failure in unprocessed:
                logger.error(
                    "Failed to archive finding %s: %s",
                    failure.get("FindingIdentifier", {}).get("Id"),
                    failure.get("ErrorMessage"),
                )
            msg = f"{len(unprocessed)} findings failed to archive"
            raise RuntimeError(msg)
    logger.info("Archived %d stale findings for %s", len(stale_ids), generator_id)


def main() -> int:
    """Parse the raw report, upload it to S3, and sync findings to Security Hub."""
    tool = _env("VULN_SCANNER_TOOL").lower()
    if tool not in PARSERS:
        msg = f"Unknown VULN_SCANNER_TOOL={tool!r}; expected one of {list(PARSERS)}"
        raise SystemExit(msg)

    target_name = _env("VULN_SCANNER_TARGET_NAME")
    target_url = _env("VULN_SCANNER_TARGET_URL")
    report_path = Path(_env("VULN_SCANNER_REPORT_PATH"))
    bucket = _env("VULN_SCANNER_S3_BUCKET")
    region = _env("AWS_REGION", required=False, default="us-east-1")
    dry_run = _env("VULN_SCANNER_DRY_RUN", required=False, default="").lower() == "true"

    if not report_path.exists():
        # A missing report is a real tooling failure (the scanner never ran
        # or crashed before writing output) -- this container's exit code
        # SHOULD reflect that, unlike the initContainer's.
        logger.error("Report file %s does not exist", report_path)
        return 1

    parsed = PARSERS[tool](report_path)
    logger.info(
        "Parsed %d alerts/matches from %s report (scan_completed=%s)",
        len(parsed.alerts),
        tool,
        parsed.scan_completed,
    )

    s3_client = boto3.client("s3", region_name=region)
    upload_raw_report(
        s3_client,
        bucket=bucket,
        tool=tool,
        target_name=target_name,
        report_path=report_path,
        scan_date=datetime.now(UTC).strftime("%Y-%m-%d"),
    )

    if not parsed.scan_completed:
        # The raw report is still uploaded above for debugging, but nothing
        # gets imported or archived: zero alerts here doesn't mean "clean",
        # it means the scan never actually reached the target (see
        # ParsedReport's docstring) -- treating that as "everything's fixed"
        # would silently archive every real, previously-tracked finding.
        # Exiting non-zero makes the CronJob's Job show as Failed instead of
        # a silently-successful empty run.
        logger.error(
            "%s scan of %s never reached the target (discovery found no "
            "URLs) -- skipping Security Hub import/archive to avoid "
            "wiping out previously-tracked findings",
            tool,
            target_name,
        )
        return 1

    generator_id = generator_id_for(tool, target_name)
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    if dry_run:
        logger.info(
            "VULN_SCANNER_DRY_RUN=true -- skipping Security Hub entirely for %d alerts",
            len(parsed.alerts),
        )
        return 0

    sts = boto3.client("sts", region_name=region)
    account_id = sts.get_caller_identity()["Account"]
    # Findings imported outside AWS's partner-integration process can only be
    # written under this account's own default product.
    product_arn = (
        f"arn:aws:securityhub:{region}:{account_id}:product/{account_id}/default"
    )

    securityhub_client = boto3.client("securityhub", region_name=region)
    # Fetched once, before building this run's findings, and reused for two
    # things: preserving each existing finding's original CreatedAt below
    # (see build_asff_finding's docstring for why that matters), and the
    # archive-diff step at the end -- avoids querying Security Hub twice.
    existing = fetch_existing_findings(
        securityhub_client, product_arn=product_arn, generator_id=generator_id
    )

    findings = [
        build_asff_finding(
            alert,
            generator_id=generator_id,
            target_name=target_name,
            target_url=target_url,
            account_id=account_id,
            region=region,
            product_arn=product_arn,
            created_at=existing.get(
                finding_id(generator_id, target_name, alert.rule_id, alert.location),
                now,
            ),
            updated_at=now,
        )
        for alert in parsed.alerts
    ]

    if findings:
        import_findings(securityhub_client, findings)
    current_ids = {f["Id"] for f in findings}
    archive_stale_findings(
        securityhub_client,
        product_arn=product_arn,
        generator_id=generator_id,
        previous_ids=set(existing.keys()),
        current_ids=current_ids,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
