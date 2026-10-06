"""Refuse a Production deploy that the closed gate issue did not approve.

Runs inside the Production job, before anything deploys. The release gate is
a trigger: closing any issue that matches its ``Release <app>`` prefix fires
the job, which then deploys whatever version last passed QA. Two issues can
both be open and match the prefix at once (a stale one left behind when a
newer release was cut, or a future infrastructure-only one), and closing the
wrong one would ship a release nobody approved. So this exits non-zero unless
the closed issue's own title names the version the job is about to deploy.

``pipeline.py`` inlines this file's text into the task (``python3 -c``), so it
must stay standard-library only and self-contained.

Environment:
    APP_NAME: the app whose release gate fired.
    GATE_FILE: ``<release-gate>/gh_issue.json`` from the gate get.
    VERSION_FILE: the file the job's ``DOCKER_TAG`` comes from.
"""

import json
import os
import re
import sys


def approved_version(app: str, title: str) -> str | None:
    """Return the version a gate issue title approves, or None if it names none.

    Accepts ``Release <app> <version>`` and ``Release <app> infrastructure @
    <version>``, matched in full so another app sharing the prefix (for
    example ``Release <app>-v2 <version>``) does not match.
    """
    shapes = (
        rf"Release {re.escape(app)} infrastructure @ (\S+)",
        rf"Release {re.escape(app)} (\S+)",
    )
    for shape in shapes:
        match = re.fullmatch(shape, title.strip())
        if match:
            return match.group(1)
    return None


def main() -> None:
    app = os.environ["APP_NAME"]
    with open(os.environ["GATE_FILE"]) as gate_file:  # noqa: PTH123
        title = (json.load(gate_file).get("issue_title") or "").strip()
    with open(os.environ["VERSION_FILE"]) as version_file:  # noqa: PTH123
        deploying = version_file.read().strip()

    approved = approved_version(app, title)
    if approved is None:
        sys.exit(
            f"Refusing to deploy {app} {deploying}: the gate issue that fired "
            f"({title!r}) does not name a version. Nothing was deployed."
        )
    if approved != deploying:
        sys.exit(
            f"Refusing to deploy {app} {deploying}: the gate issue that fired "
            f"({title!r}) approved {approved}. Nothing was deployed. To ship "
            f"{deploying}, close the release issue for {deploying}."
        )
    print(f"Gate issue {title!r} approves deploying {app} {deploying}.")  # noqa: T201


if __name__ == "__main__":
    main()
