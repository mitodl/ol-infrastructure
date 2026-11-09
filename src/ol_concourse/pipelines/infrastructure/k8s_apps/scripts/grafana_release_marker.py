"""Post a Grafana annotation marking a Production release of a Kubernetes app.

Runs in the Production deploy job right after the Pulumi step, which waits for
the rollout (celery workers with ``skip_rollout_await`` excepted). The
annotation spans the rollout: it starts at the time ``release-marker-start``
recorded just before the Pulumi step and ends now. It is an organization
annotation (no dashboard), so any dashboard can show it with a Grafana
annotation query filtered by tag.

The deploy job also runs for ol-infrastructure-only changes, which redeploy the
version the last marker names; those runs post nothing.

Tags, so a dashboard can pick the apps it cares about. Grafana's annotation tag
filter is match-all or match-any, not a mix, so the combined
``<app>/<environment>/release`` tag is what lets one any-match query show
releases of several apps in one environment:

    deploy, kind:release, app:<app>, env:<environment>, version:<version>,
    infra:<short sha>, <app>/<environment>, <app>/<environment>/release

``pipeline.py`` inlines this file's text into the task (``python3 -c``), so it
must stay standard-library only and self-contained.

Environment:
    APP_NAME: the app.
    ENVIRONMENT: the environment deployed to.
    VERSION: the app version deployed.
    STARTED_AT_FILE: file holding the rollout start, in epoch seconds.
    INFRA_REF_FILE: file holding the ol-infrastructure commit deployed.
    INFRA_REPO_URL: the ol-infrastructure repository URL.
    GRAFANA_URL: the Grafana instance.
    GRAFANA_TOKEN: a service account token that can write annotations.
"""

import json
import os
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

INFRA_SHORT_SHA_LENGTH = 9


def _release_tag(app_name: str, environment: str) -> str:
    return f"{app_name}/{environment}/release"


def _marker_tags(
    app_name: str, environment: str, version: str, infra_ref: str
) -> list[str]:
    return [
        "deploy",
        "kind:release",
        f"app:{app_name}",
        f"env:{environment}",
        f"version:{version}",
        f"infra:{infra_ref[:INFRA_SHORT_SHA_LENGTH]}",
        f"{app_name}/{environment}",
        _release_tag(app_name, environment),
    ]


def _last_marked_version(annotations: list[dict[str, Any]]) -> str | None:
    """Return the newest annotation's ``version:`` tag, or None if there is none."""
    if not annotations:
        return None
    newest = max(annotations, key=lambda annotation: annotation.get("time", 0))
    for tag in newest.get("tags") or []:
        if tag.startswith("version:"):
            return tag.removeprefix("version:")
    return None


def _marker_text(
    app_name: str, environment: str, version: str, infra_ref: str, infra_repo: str
) -> str:
    return (
        f"{app_name} {version} deployed to {environment}\n"
        f"ol-infrastructure {infra_ref[:INFRA_SHORT_SHA_LENGTH]}: "
        f"{infra_repo}/commit/{infra_ref}"
    )


def _request(
    method: str, url: str, token: str, body: dict[str, Any] | None = None
) -> Any:
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(  # noqa: S310
        url,
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
        return json.loads(response.read() or b"null")


def main() -> None:
    """Post the marker unless the last one already names this version."""
    app_name = os.environ["APP_NAME"]
    environment = os.environ["ENVIRONMENT"]
    version = os.environ["VERSION"]
    started_at_ms = int(Path(os.environ["STARTED_AT_FILE"]).read_text().strip()) * 1000
    infra_ref = Path(os.environ["INFRA_REF_FILE"]).read_text().strip()
    infra_repo = os.environ["INFRA_REPO_URL"].removesuffix(".git")
    grafana_url = os.environ["GRAFANA_URL"].rstrip("/")
    token = os.environ["GRAFANA_TOKEN"]

    query = urllib.parse.urlencode(
        {
            "type": "annotation",
            "tags": _release_tag(app_name, environment),
            "limit": 20,
        }
    )
    previous = _last_marked_version(
        _request("GET", f"{grafana_url}/api/annotations?{query}", token)
    )
    if previous == version:
        sys.stdout.write(
            f"{app_name} {version} is already marked in {environment}; "
            "this run is not a release, so no marker.\n"
        )
        return

    _request(
        "POST",
        f"{grafana_url}/api/annotations",
        token,
        {
            "time": started_at_ms,
            "timeEnd": int(time.time() * 1000),
            "tags": _marker_tags(app_name, environment, version, infra_ref),
            "text": _marker_text(app_name, environment, version, infra_ref, infra_repo),
        },
    )
    sys.stdout.write(f"Marked {app_name} {version} in {environment}.\n")


if __name__ == "__main__":
    main()
