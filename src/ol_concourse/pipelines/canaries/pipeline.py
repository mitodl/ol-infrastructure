# pyright: reportCallIssue=false
"""Render a Concourse pipeline that runs one web property's Playwright canaries.

One managed pipeline per property, named ``canary-<name>``. Which properties are
actually deployed is decided by the name list in ``meta.py``; ``pipeline_params``
below is a superset, so an entry can be prepared and reviewed here before it goes
live -- the same onboarding shape as
``src/ol_concourse/pipelines/infrastructure/simple_pulumi/``.

Read ``AGENTS.md`` in this directory before changing anything here, and
``docs/adr/0011-playwright-canary-specs-in-ol-infrastructure.md`` for why the specs
live next to this file.
"""

import json
import re
import sys
from pathlib import Path
from typing import Any, Literal

from ol_concourse.lib.models.pipeline import (
    AnonymousResource,
    Command,
    GetStep,
    Identifier,
    Input,
    Job,
    Pipeline,
    Platform,
    TaskConfig,
    TaskStep,
)
from ol_concourse.lib.resources import git_repo, schedule
from pydantic import BaseModel, model_validator

CANARY_DIRECTORY = Path(__file__).parent
# Where this directory sits in a checkout, for the git resource's watched paths.
CANARY_REPO_PATH = "src/ol_concourse/pipelines/canaries"
OL_INFRASTRUCTURE_URI = "https://github.com/mitodl/ol-infrastructure"

# Not routed through the ECR pull-through cache: that cache is configured for
# Docker Hub (see dockerhub_ecr_image_uri) and this image is on Microsoft's
# registry, which has no anonymous pull limit to work around.
PLAYWRIGHT_IMAGE_REPOSITORY = "mcr.microsoft.com/playwright"

# Browsers baked into the Playwright image, each of which playwright.config.ts
# declares a project for. Adding one here without adding the project there
# renders a pipeline that fails with "unknown project".
Browser = Literal["chromium", "firefox", "webkit"]


def playwright_image_tag(canary_directory: Path = CANARY_DIRECTORY) -> str:
    """Derive the Playwright image tag from ``package.json``'s pin.

    ``package.json`` is the single source of truth for the Playwright version, and
    the browsers are baked into the image at a revision keyed to that exact
    version. Hardcoding a tag here instead re-creates the drift that ADR 0011
    exists to make unreachable, and whose only symptom is::

        browserType.launch: Executable doesn't exist at
        /ms-playwright/chromium_headless_shell-1208/...

    which names neither the pin nor the tag.

    :param canary_directory: Directory holding ``package.json``.
    :returns: An image tag such as ``v1.62.1-noble``.
    :raises ValueError: If the pin is absent or is a range rather than an exact
        version. A range lets the installed version drift away from the image tag,
        which is the failure above.
    """
    package_json = json.loads((canary_directory / "package.json").read_text())
    pin = package_json.get("devDependencies", {}).get("@playwright/test")
    if not pin:
        msg = (
            f"@playwright/test is not pinned in {canary_directory / 'package.json'}; "
            "it is the single source of truth for the canary image tag."
        )
        raise ValueError(msg)
    if not re.fullmatch(r"\d+\.\d+\.\d+", pin):
        msg = (
            f"@playwright/test must be pinned to an exact version, got {pin!r}. "
            "A range lets the installed Playwright drift from the image tag, and "
            "the resulting failure names neither version. See ADR 0011."
        )
        raise ValueError(msg)
    return f"v{pin}-noble"


class CanaryParams(BaseModel):
    """One web property's canary pipeline.

    Attributes:
        canary_name: Property name. Becomes the pipeline name ``canary-<name>``
            and, by default, the spec directory ``specs/<name>``.
        base_url: Environment the journeys run against, e.g.
            ``https://rc.learn.mit.edu``. Passed as ``CANARY_BASE_URL``, which
            ``playwright.config.ts`` requires and never defaults.
        spec_paths: Paths passed to ``playwright test``, relative to this
            directory. Defaults to the property's whole spec directory, so
            **adding a journey needs no change here** -- drop a new
            ``specs/<name>/<journey>.spec.ts`` in and it runs. Narrow this only
            to deliberately exclude a journey from the schedule.
        browsers: Playwright projects to run. Chromium alone is the default: each
            extra browser multiplies the load this canary puts on a live
            property, and cross-browser coverage is a job for an application test
            suite, not a canary.
        credential_secret: Name of the Concourse credential holding this
            property's canary login, with ``email`` and ``password`` keys --
            surfaced to the specs as ``CANARY_USER_EMAIL`` and
            ``CANARY_USER_PASSWORD``. Leave unset for a property whose journeys
            are all anonymous. Never put a credential itself here; this file is
            public source.
        timeout: Per-test budget in milliseconds.
        expect_timeout: Per-assertion budget in milliseconds. Well under
            ``timeout`` so a failing assertion reports as itself rather than as a
            whole-test timeout.
        schedule_interval: How often the canary runs.
        schedule_start: Optional daily window start, ``HH:MM``.
        schedule_stop: Optional daily window end, ``HH:MM``.
        schedule_days: Optional days to run, e.g. ``["Monday"]``.
        branch: Branch the specs are read from.
    """

    canary_name: str
    base_url: str
    spec_paths: list[str] = []
    browsers: list[Browser] = ["chromium"]
    credential_secret: str | None = None
    timeout: int = 90_000
    expect_timeout: int = 15_000
    schedule_interval: str = "10m"
    schedule_start: str | None = None
    schedule_stop: str | None = None
    schedule_days: list[str] | None = None
    branch: str = "main"

    @model_validator(mode="after")
    def default_spec_paths_to_property_directory(self) -> "CanaryParams":
        """Run the property's whole spec directory unless told otherwise."""
        if not self.spec_paths:
            self.spec_paths = [f"specs/{self.canary_name}"]
        return self


pipeline_params: dict[str, CanaryParams] = {
    "mit-learn": CanaryParams(
        canary_name="mit-learn",
        base_url="https://rc.learn.mit.edu",
    ),
}


def build_canary_pipeline(canary_name: str) -> Pipeline:
    """Render the canary pipeline for one property.

    :param canary_name: Key into :data:`pipeline_params`.
    :returns: The pipeline to set as ``canary-<canary_name>``.
    :raises ValueError: On an unknown name, listing the available ones.
    """
    if canary_name not in pipeline_params:
        msg = (
            f"Unknown canary {canary_name!r}. "
            f"Available canaries: {', '.join(sorted(pipeline_params))}"
        )
        raise ValueError(msg)
    params = pipeline_params[canary_name]

    canary_code = git_repo(
        name=Identifier("canary-code"),
        uri=OL_INFRASTRUCTURE_URI,
        branch=params.branch,
        paths=[f"{CANARY_REPO_PATH}/"],
    )
    canary_schedule = schedule(
        name=Identifier("canary-schedule"),
        interval=params.schedule_interval,
        start=params.schedule_start,
        stop=params.schedule_stop,
        days=params.schedule_days,
    )

    task_params: dict[str, Any] = {
        "CANARY_BASE_URL": params.base_url,
        "CANARY_TIMEOUT": str(params.timeout),
        "CANARY_EXPECT_TIMEOUT": str(params.expect_timeout),
    }
    if params.credential_secret:
        # Resolved by Concourse's Vault credential manager at task start, so the
        # value never appears in this pipeline's definition.json.
        task_params["CANARY_USER_EMAIL"] = f"(({params.credential_secret}.email))"
        task_params["CANARY_USER_PASSWORD"] = f"(({params.credential_secret}.password))"

    project_flags = " ".join(f"--project={browser}" for browser in params.browsers)
    spec_arguments = " ".join(params.spec_paths)
    run_canary = "\n".join(
        [
            "set -euo pipefail",
            f"cd canary-code/{CANARY_REPO_PATH}",
            # @playwright/test is not installed globally in the image, so this is
            # mandatory. It is also cheap -- 6 packages, no browser download,
            # because the browsers are already in the image -- which is why no
            # bespoke canary image is built. Keep it that way (see AGENTS.md).
            "npm ci",
            f"npx playwright test {spec_arguments} {project_flags}",
        ]
    )

    return Pipeline(
        resources=[canary_code, canary_schedule],
        jobs=[
            Job(
                name=Identifier(f"run-{params.canary_name}-canary"),
                # A canary that piles up behind a slow run reports on a target it
                # is also still loading, and the failures interleave.
                max_in_flight=1,
                plan=[
                    GetStep(get=canary_schedule.name, trigger=True),
                    GetStep(get=canary_code.name, trigger=True),
                    TaskStep(
                        task=Identifier(f"run-{params.canary_name}-journeys"),
                        config=TaskConfig(
                            platform=Platform.linux,
                            image_resource=AnonymousResource(
                                type="registry-image",
                                source={
                                    "repository": PLAYWRIGHT_IMAGE_REPOSITORY,
                                    "tag": playwright_image_tag(),
                                },
                            ),
                            inputs=[Input(name=canary_code.name)],
                            params=task_params,
                            run=Command(
                                path="bash",
                                args=["-c", run_canary],
                            ),
                        ),
                    ),
                ],
            )
        ],
    )


if __name__ == "__main__":
    min_args = 2
    if len(sys.argv) < min_args:
        msg = (
            "Please provide a canary name as a command line argument.\n"
            f"Available canaries: {', '.join(sorted(pipeline_params))}"
        )
        raise ValueError(msg)

    canary_name = sys.argv[1]

    try:
        pipeline_json = build_canary_pipeline(canary_name).model_dump_json(indent=2)
        with open("definition.json", "w") as definition:  # noqa: PTH123
            definition.write(pipeline_json)
        sys.stdout.write(pipeline_json)
        print()  # noqa: T201
        print(f"fly -t pr-inf sp -p canary-{canary_name} -c definition.json")  # noqa: T201
    except ValueError as error:
        sys.stderr.write(f"Error: {error}\n")
        sys.exit(1)
