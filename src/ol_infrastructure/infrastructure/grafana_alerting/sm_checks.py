"""Grafana Synthetic Monitoring checks.

Replaces Pingdom. All checks run in the production Grafana Cloud stack only —
even checks that target non-production URLs (e.g. QA or RC environments).
Centralising them in one stack avoids duplicating probe config, alert
sensitivity settings, and notification routing across three stacks.

Probes: Atlanta, Frankfurt, Singapore, Sydney (resolved by name at deploy time
so probe IDs are never hardcoded).

Skipped checks (paused/dead in Pingdom, not worth migrating):
  - ChrisTestNukeMe           plaintext.reedfish-regulus.ts.net  (test check)
  - xPro preview production   preview.xpro.mit.edu               (dead since 2022)
  - MITx production CMS       studio.mitx.mit.edu  (paused 2023, superseded by
                                                   "MITx production Studio")
"""

from dataclasses import dataclass

from pulumi import InvokeOptions, ResourceOptions
from pulumiverse_grafana import syntheticmonitoring


@dataclass
class _SMCheck:
    resource_name: str
    job: str
    target: str
    frequency: int  # milliseconds
    alert_sensitivity: str  # "low" or "high"
    labels: dict[str, str]


_CHECKS: list[_SMCheck] = [
    # --- Airbyte ---
    _SMCheck(
        resource_name="airbyte-qa-http",
        job="Airbyte QA",
        target="https://airbyte-qa.odl.mit.edu/api/v1/health",
        frequency=60000,
        alert_sensitivity="low",
        labels={"env": "qa", "service": "airbyte"},
    ),
    # --- Concourse ---
    _SMCheck(
        resource_name="concourse-production-http",
        job="Concourse Production",
        target="https://cicd.odl.mit.edu/",
        frequency=60000,
        alert_sensitivity="high",
        labels={"env": "production", "service": "concourse"},
    ),
    # --- Dagster ---
    _SMCheck(
        resource_name="dagster-qa-http",
        job="Dagster QA",
        target="https://pipelines-qa.odl.mit.edu/healthcheck/test_dagster.py",
        frequency=60000,
        alert_sensitivity="low",
        labels={"env": "qa", "service": "dagster"},
    ),
    _SMCheck(
        resource_name="dagster-production-http",
        job="Dagster Production",
        target="https://pipelines.odl.mit.edu/healthcheck/test_dagster.py",
        frequency=60000,
        alert_sensitivity="high",
        labels={"env": "production", "service": "dagster"},
    ),
    # --- Learn ---
    _SMCheck(
        resource_name="learn-api-production-http",
        job="Learn API Production",
        target="https://api.learn.mit.edu/learn/health/",
        frequency=60000,
        alert_sensitivity="high",
        labels={"env": "production", "service": "learn"},
    ),
    _SMCheck(
        resource_name="learn-api-rc-http",
        job="Learn API RC",
        target="https://api.rc.learn.mit.edu/learn/health/",
        frequency=60000,
        alert_sensitivity="low",
        labels={"env": "rc", "service": "learn"},
    ),
    _SMCheck(
        resource_name="learn-production-http",
        job="Learn Production",
        target="https://learn.mit.edu/",
        frequency=60000,
        alert_sensitivity="high",
        labels={"env": "production", "service": "learn"},
    ),
    _SMCheck(
        resource_name="learn-qa-http",
        job="Learn QA",
        target="https://next.rc.learn.mit.edu/",
        frequency=60000,
        alert_sensitivity="low",
        labels={"env": "rc", "service": "learn"},
    ),
    # --- Learn AI ---
    _SMCheck(
        resource_name="learn-ai-api-production-http",
        job="Learn AI API Production",
        target="https://api.learn.mit.edu/ai/health/readiness/",
        frequency=60000,
        alert_sensitivity="high",
        labels={"env": "production", "service": "learn-ai"},
    ),
    _SMCheck(
        resource_name="learn-ai-api-qa-http",
        job="Learn AI API QA",
        target="https://api-learn-ai-qa.ol.mit.edu/api/v0/schema/swagger-ui/",
        frequency=300000,
        alert_sensitivity="low",
        labels={"env": "qa", "service": "learn-ai"},
    ),
    _SMCheck(
        resource_name="learn-ai-frontend-qa-http",
        job="Learn AI Frontend QA",
        target="https://learn-ai-qa.ol.mit.edu/",
        frequency=300000,
        alert_sensitivity="low",
        labels={"env": "qa", "service": "learn-ai"},
    ),
    # --- Micromasters ---
    _SMCheck(
        resource_name="micromasters-production-http",
        job="Micromasters Production",
        target="https://micromasters.mit.edu/",
        frequency=60000,
        alert_sensitivity="high",
        labels={"env": "production", "service": "micromasters"},
    ),
    _SMCheck(
        resource_name="micromasters-rc-http",
        job="Micromasters RC",
        target="https://micromasters-rc.odl.mit.edu/",
        frequency=60000,
        alert_sensitivity="low",
        labels={"env": "rc", "service": "micromasters"},
    ),
    # --- MITx (residential) ---
    _SMCheck(
        resource_name="mitx-lms-production-http",
        job="MITx Production LMS",
        target="https://lms.mitx.mit.edu/",
        frequency=60000,
        alert_sensitivity="high",
        labels={"env": "production", "service": "mitx"},
    ),
    _SMCheck(
        resource_name="mitx-lms-qa-http",
        job="MITx QA LMS",
        target="https://mitx-qa.mitx.mit.edu/",
        frequency=60000,
        alert_sensitivity="low",
        labels={"env": "qa", "service": "mitx"},
    ),
    _SMCheck(
        resource_name="mitx-lms-staging-http",
        job="MITx Production LMS Draft",
        target="https://staging.mitx.mit.edu/",
        frequency=60000,
        alert_sensitivity="low",
        labels={"env": "staging", "service": "mitx"},
    ),
    _SMCheck(
        resource_name="mitx-cms-qa-http",
        job="MITx QA CMS",
        target="https://studio-mitx-qa.mitx.mit.edu/",
        frequency=60000,
        alert_sensitivity="low",
        labels={"env": "qa", "service": "mitx"},
    ),
    _SMCheck(
        resource_name="mitx-cms-staging-http",
        job="MITx Production CMS Draft",
        target="https://studio-staging.mitx.mit.edu/",
        frequency=60000,
        alert_sensitivity="low",
        labels={"env": "staging", "service": "mitx"},
    ),
    _SMCheck(
        resource_name="mitx-studio-production-http",
        job="MITx Production Studio",
        target="https://studio.mitx.mit.edu/",
        frequency=60000,
        alert_sensitivity="high",
        labels={"env": "production", "service": "mitx"},
    ),
    # --- MITx Online ---
    _SMCheck(
        resource_name="mitxonline-edx-production-http",
        job="MITx Online Open edX Production",
        target="https://courses.mitxonline.mit.edu/heartbeat",
        frequency=60000,
        alert_sensitivity="high",
        labels={"env": "production", "service": "mitxonline"},
    ),
    _SMCheck(
        resource_name="mitxonline-edx-rc-http",
        job="MITx Online QA edX",
        target="https://courses.rc.mitxonline.mit.edu/heartbeat",
        frequency=60000,
        alert_sensitivity="low",
        labels={"env": "rc", "service": "mitxonline"},
    ),
    _SMCheck(
        resource_name="mitxonline-production-http",
        job="MITx Online Production",
        target="https://mitxonline.mit.edu/",
        frequency=60000,
        alert_sensitivity="high",
        labels={"env": "production", "service": "mitxonline"},
    ),
    _SMCheck(
        resource_name="mitxonline-rc-http",
        job="MITx Online RC",
        target="https://rc.mitxonline.mit.edu/",
        frequency=60000,
        alert_sensitivity="low",
        labels={"env": "rc", "service": "mitxonline"},
    ),
    # --- OCW ---
    _SMCheck(
        resource_name="ocw-production-http",
        job="OCW Production",
        target="https://ocw.mit.edu/",
        frequency=60000,
        alert_sensitivity="high",
        labels={"env": "production", "service": "ocw"},
    ),
    _SMCheck(
        resource_name="ocw-legacy-http",
        job="Legacy OCW",
        target="https://old.ocw.mit.edu/",
        frequency=60000,
        alert_sensitivity="high",
        labels={"env": "production", "service": "ocw"},
    ),
    _SMCheck(
        resource_name="ocw-studio-production-http",
        job="OCW Studio",
        target="https://ocw-studio.odl.mit.edu/",
        frequency=60000,
        alert_sensitivity="high",
        labels={"env": "production", "service": "ocw-studio"},
    ),
    # --- ODL Video ---
    _SMCheck(
        resource_name="odl-video-production-http",
        job="ODL Video Production",
        target="https://video.odl.mit.edu/terms",
        frequency=60000,
        alert_sensitivity="high",
        labels={"env": "production", "service": "odl-video"},
    ),
    _SMCheck(
        resource_name="odl-video-rc-http",
        job="ODL Video RC",
        target="https://video-rc.odl.mit.edu/terms",
        frequency=60000,
        alert_sensitivity="low",
        labels={"env": "rc", "service": "odl-video"},
    ),
    # --- Open Discussions ---
    _SMCheck(
        resource_name="open-discussions-production-http",
        job="Open Discussions Production",
        target="https://open.mit.edu/",
        frequency=60000,
        alert_sensitivity="high",
        labels={"env": "production", "service": "open-discussions"},
    ),
    # --- SSO (Keycloak) ---
    _SMCheck(
        resource_name="sso-production-olapps-http",
        job="SSO Production - Apps Realm",
        target="https://sso.ol.mit.edu/realms/olapps/account/",
        frequency=60000,
        alert_sensitivity="high",
        labels={"env": "production", "service": "sso"},
    ),
    _SMCheck(
        resource_name="sso-production-data-http",
        job="SSO Production - Data Realm",
        target="https://sso.ol.mit.edu/realms/ol-data-platform/account/",
        frequency=60000,
        alert_sensitivity="high",
        labels={"env": "production", "service": "sso"},
    ),
    _SMCheck(
        resource_name="sso-qa-olapps-http",
        job="SSO QA - Open Realm",
        target="https://sso-qa.ol.mit.edu/realms/olapps/account/",
        frequency=60000,
        alert_sensitivity="low",
        labels={"env": "qa", "service": "sso"},
    ),
    _SMCheck(
        resource_name="sso-qa-data-http",
        job="SSO QA - Data Realm",
        target="https://sso-qa.ol.mit.edu/realms/ol-data-platform/account/",
        frequency=60000,
        alert_sensitivity="low",
        labels={"env": "qa", "service": "sso"},
    ),
    # --- xPro ---
    # Note: xpro.mit.edu and studio.xpro.mit.edu were both DOWN at Pingdom
    # export time and will alert immediately once enabled in production.
    _SMCheck(
        resource_name="xpro-production-http",
        job="xPro Production",
        target="https://xpro.mit.edu/",
        frequency=60000,
        alert_sensitivity="high",
        labels={"env": "production", "service": "xpro"},
    ),
    _SMCheck(
        resource_name="xpro-lms-production-http",
        job="xPro LMS Production",
        target="https://courses.xpro.mit.edu/heartbeat",
        frequency=60000,
        alert_sensitivity="high",
        labels={"env": "production", "service": "xpro"},
    ),
    _SMCheck(
        resource_name="xpro-cms-production-http",
        job="xPro CMS Production",
        target="https://studio.xpro.mit.edu/",
        frequency=60000,
        alert_sensitivity="high",
        labels={"env": "production", "service": "xpro"},
    ),
    _SMCheck(
        resource_name="xpro-rc-http",
        job="xPro RC",
        target="https://rc.xpro.mit.edu/",
        frequency=60000,
        alert_sensitivity="low",
        labels={"env": "rc", "service": "xpro"},
    ),
    _SMCheck(
        resource_name="xpro-lms-rc-http",
        job="xPro LMS RC",
        target="https://courses-rc.xpro.mit.edu/heartbeat",
        frequency=60000,
        alert_sensitivity="low",
        labels={"env": "rc", "service": "xpro"},
    ),
    _SMCheck(
        resource_name="xpro-cms-rc-http",
        job="xPro CMS RC",
        target="https://studio-rc.xpro.mit.edu/heartbeat",
        frequency=60000,
        alert_sensitivity="low",
        labels={"env": "rc", "service": "xpro"},
    ),
]

_DESIRED_PROBE_NAMES = {"Atlanta", "Frankfurt", "Singapore", "Sydney"}


def create(invoke_opts: InvokeOptions, resource_opts: ResourceOptions) -> None:
    """Create all Synthetic Monitoring uptime checks in the production Grafana stack."""
    # Resolve probe names to IDs at deploy time so numeric IDs are never hardcoded.
    all_probes = syntheticmonitoring.get_probes(opts=invoke_opts)
    selected_probe_ids = [
        probe_id
        for name, probe_id in all_probes.probes.items()
        if name in _DESIRED_PROBE_NAMES
    ]

    for check in _CHECKS:
        syntheticmonitoring.Check(
            check.resource_name,
            job=check.job,
            target=check.target,
            enabled=True,
            frequency=check.frequency,
            timeout=10000,
            probes=selected_probe_ids,
            settings=syntheticmonitoring.CheckSettingsArgs(
                http=syntheticmonitoring.CheckSettingsHttpArgs(
                    valid_status_codes=[200],
                    fail_if_not_ssl=True,
                ),
            ),
            labels=check.labels,
            alert_sensitivity=check.alert_sensitivity,
            opts=resource_opts,
        )
