"""Pingdom uptime checks managed via a Pulumi dynamic provider.

Replaces Grafana Synthetic Monitoring (too expensive at 4-probe x 1-min cadence).
Checks are Pingdom account-wide resources, so they are created in the production
stack only to avoid duplicates across CI/QA/Production Pulumi stacks.

Production checks use 2 probe regions (NA + EU).
Non-production checks use 1 probe region (NA).

Skipped checks (paused/dead in Pingdom at migration time, not worth monitoring):
  - ChrisTestNukeMe           plaintext.reedfish-regulus.ts.net  (test check)
  - xPro preview production   preview.xpro.mit.edu               (dead since 2022)
  - MITx production CMS       studio.mitx.mit.edu  (paused 2023, superseded by
                                                   "MITx production Studio")

xPro production checks are created as paused=True — they were DOWN at the time
of migration and will alert immediately if enabled without investigation first.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import requests
from pulumi import Config, Input, ResourceOptions
from pulumi.dynamic import (
    CreateResult,
    ReadResult,
    Resource,
    ResourceProvider,
    UpdateResult,
)

_API_BASE = "https://api.pingdom.com/api/3.1"
_REQUEST_TIMEOUT = 10  # seconds
_HTTP_NOT_FOUND = 404

_PROD_PROBE_FILTERS = ["region:NA", "region:EU"]
_NON_PROD_PROBE_FILTERS = ["region:NA"]


class _PingdomCheckProvider(ResourceProvider):
    """Pulumi dynamic provider for a single Pingdom HTTP check (v3 REST API)."""

    @staticmethod
    def _auth(props: dict[str, Any]) -> dict[str, str]:
        return {"Authorization": f"Bearer {props['api_token']}"}

    @staticmethod
    def _body(props: dict[str, Any]) -> dict[str, Any]:
        return {
            "name": props["name"],
            "host": props["host"],
            "type": "http",
            "url": props["url"],
            "port": 443,
            "encryption": True,
            "resolution": props["resolution"],
            "probe_filters": props["probe_filters"],
            "tags": props["tags"],
            "integrationids": props["integrationids"],
            "paused": props.get("paused", False),
        }

    def create(self, props: dict[str, Any]) -> CreateResult:
        """Create a new Pingdom check and return its numeric ID as the resource ID."""
        r = requests.post(
            f"{_API_BASE}/checks",
            headers=self._auth(props),
            json=self._body(props),
            timeout=_REQUEST_TIMEOUT,
        )
        r.raise_for_status()
        check_id = str(r.json()["check"]["id"])
        return CreateResult(id_=check_id, outs={**props, "check_id": check_id})

    def read(self, id_: str, props: dict[str, Any]) -> ReadResult:
        """Refresh state by fetching the check from Pingdom."""
        r = requests.get(
            f"{_API_BASE}/checks/{id_}",
            headers=self._auth(props),
            timeout=_REQUEST_TIMEOUT,
        )
        if r.status_code == _HTTP_NOT_FOUND:
            return ReadResult(id_=None, outs={})
        r.raise_for_status()
        return ReadResult(id_=id_, outs=props)

    def update(
        self, id_: str, _olds: dict[str, Any], news: dict[str, Any]
    ) -> UpdateResult:
        """Update an existing Pingdom check. Type cannot be changed after creation."""
        body = self._body(news)
        body.pop("type", None)
        r = requests.put(
            f"{_API_BASE}/checks/{id_}",
            headers=self._auth(news),
            json=body,
            timeout=_REQUEST_TIMEOUT,
        )
        r.raise_for_status()
        return UpdateResult(outs={**news, "check_id": id_})

    def delete(self, id_: str, props: dict[str, Any]) -> None:
        """Delete the Pingdom check. A 404 means it is already gone."""
        r = requests.delete(
            f"{_API_BASE}/checks/{id_}",
            headers=self._auth(props),
            timeout=_REQUEST_TIMEOUT,
        )
        if r.status_code == _HTTP_NOT_FOUND:
            return
        r.raise_for_status()


class _PingdomCheck(Resource):
    """A single Pingdom HTTP uptime check as a Pulumi resource."""

    def __init__(  # noqa: PLR0913
        self,
        resource_name: str,
        *,
        api_token: Input[str],
        name: str,
        host: str,
        url: str,
        resolution: int,
        probe_filters: list[str],
        tags: list[str],
        integrationids: list[int],
        paused: bool = False,
        opts: ResourceOptions | None = None,
    ) -> None:
        super().__init__(
            _PingdomCheckProvider(),
            resource_name,
            {
                "api_token": api_token,
                "name": name,
                "host": host,
                "url": url,
                "resolution": resolution,
                "probe_filters": probe_filters,
                "tags": tags,
                "integrationids": integrationids,
                "paused": paused,
                "check_id": None,
            },
            opts,
        )


@dataclass
class _SMCheck:
    resource_name: str
    job: str
    target: str
    frequency: int  # milliseconds; converted to minutes (resolution) for Pingdom
    alert_sensitivity: str  # "high" -> PROD probes, "low" -> NON_PROD probes
    labels: dict[str, str]
    paused: bool = False


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
        job="SSO QA - Apps Realm",
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
    # paused=True: xpro.mit.edu, courses.xpro.mit.edu, and studio.xpro.mit.edu
    # were all DOWN at migration time. Enable after investigating root cause.
    _SMCheck(
        resource_name="xpro-production-http",
        job="xPro Production",
        target="https://xpro.mit.edu/",
        frequency=60000,
        alert_sensitivity="high",
        labels={"env": "production", "service": "xpro"},
        paused=True,
    ),
    _SMCheck(
        resource_name="xpro-lms-production-http",
        job="xPro LMS Production",
        target="https://courses.xpro.mit.edu/heartbeat",
        frequency=60000,
        alert_sensitivity="high",
        labels={"env": "production", "service": "xpro"},
        paused=True,
    ),
    _SMCheck(
        resource_name="xpro-cms-production-http",
        job="xPro CMS Production",
        target="https://studio.xpro.mit.edu/",
        frequency=60000,
        alert_sensitivity="high",
        labels={"env": "production", "service": "xpro"},
        paused=True,
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


def create(api_token: Input[str], integration_ids: list[int]) -> None:
    """Create all Pingdom HTTP uptime checks.

    The 39 checks below already exist, live and correctly configured, in the
    production Pingdom account -- but are NOT tracked in this stack's Pulumi
    state (see docs/adr/0010-pingdom-checks-unmanaged-in-pulumi-state.md for
    why `pulumi import` cannot be used to fix this). Running this function
    against a stack that has none of them in state will attempt to create 39
    duplicates. Requiring an explicit opt-in here prevents that from
    happening as a side effect of an unrelated `pulumi up`.
    """
    if not Config().get_bool("allow_pingdom_apply"):
        msg = (
            "Pingdom checks are live in Pingdom but unmanaged in Pulumi "
            "state -- see docs/adr/0010-pingdom-checks-unmanaged-in-pulumi-"
            "state.md. Refusing to run to avoid creating 39 duplicate "
            "checks. Read that ADR, then set "
            "`pulumi config set allow_pingdom_apply true` if you intend to "
            "proceed anyway (e.g. to add a single new check with "
            "--target)."
        )
        raise RuntimeError(msg)

    for check in _CHECKS:
        parsed = urlparse(check.target)
        host = parsed.hostname
        if not host:
            msg = f"Could not parse hostname from target URL: {check.target}"
            raise ValueError(msg)
        _PingdomCheck(
            check.resource_name,
            api_token=api_token,
            name=check.job,
            host=host,
            url=parsed.path or "/",
            resolution=check.frequency // 60000,
            probe_filters=(
                _PROD_PROBE_FILTERS
                if check.alert_sensitivity == "high"
                else _NON_PROD_PROBE_FILTERS
            ),
            tags=[v for v in check.labels.values() if v],
            integrationids=integration_ids,
            paused=check.paused,
        )
