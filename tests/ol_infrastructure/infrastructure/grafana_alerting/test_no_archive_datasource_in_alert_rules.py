"""The telemetry archive is never a datasource for an alert rule or a dashboard.

ADR-0014 makes the archive best effort: writers drop its copy rather than wait,
and it has no on-call. A rule reading it would page on the archive's gaps, and a
dashboard panel on it would show those gaps as outages. Plan D13 fixes the
datasource UIDs so this test can refuse them by name across every module that
builds rules or dashboards.
"""

from pathlib import Path

from ol_infrastructure.infrastructure import grafana_alerting
from ol_infrastructure.lib import telemetry_archive
from ol_infrastructure.lib.telemetry_archive import (
    ARCHIVE_LOKI_DATASOURCE_UID,
    ARCHIVE_MIMIR_DATASOURCE_UID,
)

PACKAGE_DIR = Path(grafana_alerting.__file__).parent
SCANNED_PACKAGES = ("metric_rules", "log_rules", "dashboards")
# The UIDs themselves, the constants that hold them, and the module that
# exports them: importing the constant is the one-line way around a scan for
# the literal.
FORBIDDEN = (
    ARCHIVE_LOKI_DATASOURCE_UID,
    ARCHIVE_MIMIR_DATASOURCE_UID,
    "ARCHIVE_LOKI_DATASOURCE_UID",
    "ARCHIVE_MIMIR_DATASOURCE_UID",
    telemetry_archive.__name__,
)


def _sources() -> list[Path]:
    return [
        path
        for package in SCANNED_PACKAGES
        for path in (PACKAGE_DIR / package).rglob("*.py")
    ]


def test_scans_every_rule_and_dashboard_package() -> None:
    """A renamed package would otherwise make the check below pass vacuously."""
    assert {path.parent.name for path in _sources()} >= set(SCANNED_PACKAGES)


def test_no_rule_or_dashboard_references_an_archive_datasource() -> None:
    """Plan D13: no rule or dashboard module names an archive UID or imports them."""
    offenders = [
        f"{path.relative_to(PACKAGE_DIR)}: {uid}"
        for path in _sources()
        for uid in FORBIDDEN
        if uid in path.read_text()
    ]
    assert offenders == [], (
        "Archive datasources are read-only for people (ADR-0014, plan D13): "
        + "; ".join(offenders)
    )
