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
import textwrap
from pathlib import Path
from typing import Any, Literal

from ol_concourse.lib.models.pipeline import (
    AnonymousResource,
    Command,
    GetStep,
    Identifier,
    InParallelStep,
    Input,
    Job,
    LoadVarStep,
    Output,
    Pipeline,
    Platform,
    PutStep,
    Resource,
    TaskConfig,
    TaskStep,
    TryStep,
)
from ol_concourse.lib.resource_types import rclone
from ol_concourse.lib.resources import git_repo, schedule
from pydantic import BaseModel, model_validator

from ol_concourse.pipelines.canary_verdicts import (
    CANARY_VERDICT_PREFIX,
    UNATTRIBUTABLE_RELEASE_REFS,
    canary_verdict_key,
)
from ol_concourse.pipelines.deploy_markers import (
    RC_ENVIRONMENT,
    deploy_marker_resource,
)

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

# Failure artifacts land here. Already writable by the operations Concourse
# workers' instance role (see applications/concourse/iam_policies/operations.py),
# which is why the rclone resource below can use env_auth and carry no keys.
ARTIFACT_BUCKET = "ol-eng-artifacts"
ARTIFACT_PREFIX = "canary-results"
# The task output the specs' traces, screenshots and video are collected into,
# and the directory rclone uploads.
ARTIFACT_OUTPUT = Identifier("canary-results")

# Playwright's machine-readable report, uploaded on *every* run rather than only
# on failure, and kept under its own prefix so listing the run history is one
# cheap prefix scan that does not interleave with multi-megabyte failure trees.
#
# This exists because a flake is invisible in every other record. A journey that
# fails its first attempt and passes on the retry exits 0 -- measured, not assumed
# -- so the build is green, the failure-only upload publishes nothing, and
# Playwright's "N flaky" line survives only in a task log Concourse reaps within a
# handful of builds. results.json carries `stats.flaky` and the per-attempt status
# of every test, so retaining it makes the green-run flake rate answerable by
# reading the bucket.
#
# It is evidence, not a second result signal: Concourse build status remains the
# only canary result. Do not grow a metric push, Grafana alert or notification off
# the back of this.
REPORT_PREFIX = "canary-runs"
REPORT_OUTPUT = Identifier("canary-report")

# The per-release verdict, written on both the green and the red path for any
# canary that can name the release it tested, and read by the production deploy
# gate in ``infrastructure/k8s_apps/pipeline.py``. See
# :mod:`ol_concourse.pipelines.canary_verdicts` for the key layout and for why
# this is a file the task writes rather than a Concourse resource.
#
# Unlike the two uploads above, something downstream *acts* on this one. That
# does not make it a second result signal -- there is still no metric, alert or
# notification anywhere, and the gate's own verdict is a Concourse job going red
# exactly like every other promotion check.
VERDICT_OUTPUT = Identifier("canary-verdict")

# Surfaced to the specs and recorded into results.json via playwright.config.ts's
# `metadata`. Exported unconditionally: a run with no deploy trigger configured
# still names what it believes it tested, rather than leaving the field missing
# and indistinguishable from a run that failed to resolve it.
UNKNOWN_RELEASE_REF = "unknown"


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


class DeployTrigger(BaseModel):
    """Run this property's canary when a deploy to its environment finishes.

    A canary that only runs on a timer notices a bad release somewhere in the
    next interval and cannot say which release it was testing. This adds the
    deploy half of issue #5592's "after a new release to RC and also periodically
    on a schedule" **without replacing the schedule** -- both feed the same job,
    because breakage that no deploy caused is the other half of what a canary is
    for.

    The mechanism is the shared S3 deploy marker described in
    :mod:`ol_concourse.pipelines.deploy_markers`. It is a marker rather than a
    `passed` constraint because `passed` is pipeline-local and the deploy runs in
    a different pipeline, and rather than the app's GitHub Deployment because the
    ``github-deployments`` resource is put-only (``check_every: never``) and only
    the release-resource pipeline shape creates one at all -- which ``mit-learn``,
    on the legacy shape, does not.

    The producing side is opt-in too: the app needs
    ``AppPipelineParams.publish_rc_deploy_marker`` set in
    ``infrastructure/k8s_apps/pipeline.py``. Setting this field without that one
    yields a canary that keeps running on its schedule and reports
    ``0.0.0.0`` as the release it tested -- degraded, but never stuck, which is
    the whole reason the marker resource declares an initial version.

    Attributes:
        app_name: The application name as ``k8s_apps`` knows it, which is the
            marker's path segment. Often but not always the canary name: a
            property can be served by an app under a different name.
        environment: Environment whose deploys trigger the canary. Must match the
            environment the producer publishes under, and must be the one
            ``CanaryParams.base_url`` points at -- a canary triggered by a
            production deploy while pointed at RC reports on the wrong release.
    """

    app_name: str
    environment: str = RC_ENVIRONMENT


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
        deploy_trigger: Optionally also run this canary when a deploy to its
            environment finishes, in addition to the schedule. See
            :class:`DeployTrigger`. Left unset, the canary is schedule-only and
            nothing else about the pipeline changes.
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
    deploy_trigger: DeployTrigger | None = None
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
        # The Concourse credential's NAME, not a credential. Resolves from Vault at
        # secret-concourse/infrastructure/canary_mit_learn for pr-inf pipelines.
        credential_secret="canary_mit_learn",  # noqa: S106  # pragma: allowlist secret
        # base_url above is rc.learn.mit.edu, so the trigger is RC deploys of the
        # mit-learn app. Paired with publish_rc_deploy_marker=True on the
        # "mit-learn" AppPipelineParams entry in
        # infrastructure/k8s_apps/pipeline.py -- both halves are required.
        deploy_trigger=DeployTrigger(app_name="mit-learn"),
    ),
}


def _release_verdict_lines(params: CanaryParams, artifact_run_prefix: str) -> list[str]:
    """Return the shell that records this run's verdict on the release it tested.

    Emitted only for a canary with a ``deploy_trigger``: without one there is no
    release identity, and a verdict about a release nobody can name is a file
    that can only ever mislead.

    Written **after** the test run and on both paths -- a red run records a
    ``fail`` verdict, not merely an absent ``pass`` -- so the gate that reads
    these can tell "the canary tested this release and it was broken" from "the
    canary has not tested this release at all". Both block a promotion, but they
    call for different actions and the gate says which it is.

    Nothing here can change the canary's own result: the verdict is written from
    ``$canary_status``, and the exit at the end of the script re-raises it
    untouched.

    :param params: The canary being rendered.
    :param artifact_run_prefix: ``<pipeline>/<job>``, used to point the verdict
        at this run's retained ``results.json``.
    :returns: Lines to splice into the canary task script, or an empty list.
    """
    if not params.deploy_trigger:
        return []
    app_name = params.deploy_trigger.app_name
    environment = params.deploy_trigger.environment
    # The local tree under VERDICT_OUTPUT mirrors the object key *below* the
    # prefix, because the rclone destination below already carries the prefix.
    # Built from canary_verdict_key rather than spelled out so the producer
    # cannot drift from the gate that reads it.
    verdict_relative_key = canary_verdict_key(
        app_name, environment, "${CANARY_RELEASE_REF}", "${verdict}"
    ).removeprefix(f"{CANARY_VERDICT_PREFIX}/")
    unattributable = "|".join(f'"{ref}"' for ref in UNATTRIBUTABLE_RELEASE_REFS)
    results_object = (
        f"s3://{ARTIFACT_BUCKET}/{REPORT_PREFIX}/{artifact_run_prefix}/$run_stamp.json"
    )
    return [
        'if [ "$canary_status" -eq 0 ]; then',
        "  verdict=pass",
        "else",
        "  verdict=fail",
        "fi",
        # A canary with a deploy trigger still reports the marker's synthetic
        # initial version until the app has published a real one, and that is
        # not a release anything can promote.
        'case "$CANARY_RELEASE_REF" in',
        f"  {unattributable})",
        "    echo \"Release ref is '$CANARY_RELEASE_REF'; recording no verdict.\" ;;",
        "  *)",
        f'    verdict_file="$verdict_root/{verdict_relative_key}"',
        '    mkdir -p "$(dirname "$verdict_file")"',
        '    cat > "$verdict_file" <<EOF',
        "{",
        f'  "app": "{app_name}",',
        f'  "environment": "{environment}",',
        '  "release": "$CANARY_RELEASE_REF",',
        '  "outcome": "$verdict",',
        f'  "canary": "canary-{params.canary_name}",',
        '  "run": "$run_stamp",',
        '  "recorded_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",',
        f'  "results": "{results_object}"',
        "}",
        "EOF",
        '    cat "$verdict_file" ;;',
        "esac",
    ]


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
    deploy_marker: Resource | None = None
    if params.deploy_trigger:
        deploy_marker = deploy_marker_resource(
            name=Identifier("deploy-marker"),
            app_name=params.deploy_trigger.app_name,
            environment=params.deploy_trigger.environment,
        )

    task_params: dict[str, Any] = {
        "CANARY_BASE_URL": params.base_url,
        "CANARY_TIMEOUT": str(params.timeout),
        "CANARY_EXPECT_TIMEOUT": str(params.expect_timeout),
        # Recorded into results.json by playwright.config.ts, so the retained
        # per-run record says which release it covered. Concourse's own build
        # page shows the same thing as the deploy-marker resource version; this
        # carries it into the artifact, which outlives the build.
        "CANARY_RELEASE_REF": (
            "((.:release_ref))" if deploy_marker else UNKNOWN_RELEASE_REF
        ),
    }
    if params.credential_secret:
        # Resolved by Concourse's Vault credential manager at task start, so the
        # value never appears in this pipeline's definition.json.
        task_params["CANARY_USER_EMAIL"] = f"(({params.credential_secret}.email))"
        task_params["CANARY_USER_PASSWORD"] = f"(({params.credential_secret}.password))"

    # Uploaded by the ensure hook on the canary task below. The report goes up
    # on every run; the verdict exists only for a canary that can name the
    # release it tested, and a canary without one renders exactly the pipeline
    # it rendered before verdicts existed rather than a no-op upload 144 times
    # a day.
    evidence_uploads = [
        (REPORT_OUTPUT, REPORT_PREFIX),
        *([(VERDICT_OUTPUT, CANARY_VERDICT_PREFIX)] if params.deploy_trigger else []),
    ]

    project_flags = " ".join(f"--project={browser}" for browser in params.browsers)
    spec_arguments = " ".join(params.spec_paths)
    canary_job = Identifier(f"run-{params.canary_name}-canary")
    # Rendered in rather than read from the environment: task containers on our
    # Concourse get ATC_EXTERNAL_URL and no BUILD_* metadata at all, so a script
    # expanding $BUILD_PIPELINE_NAME dies on `set -u` before a single test runs.
    # The put step's own container does have them, but a `put` cannot interpolate
    # them into a destination, so neither end can supply the build number.
    artifact_run_prefix = f"canary-{params.canary_name}/{canary_job}"
    verdict_lines = _release_verdict_lines(params, artifact_run_prefix)
    # Resolved before the `cd` for the same reason the two above are, and only
    # when there is a verdict to write.
    verdict_root_lines = (
        [f'verdict_root="$PWD/{VERDICT_OUTPUT}"', 'mkdir -p "$verdict_root"']
        if verdict_lines
        else []
    )
    run_canary = "\n".join(
        [
            "set -euo pipefail",
            # One timestamp for both destinations, so a red build's failure tree
            # and its results.json record carry the same name and can be matched
            # to each other without guessing.
            'run_stamp="$(date -u +%Y%m%dT%H%M%SZ)"',
            # Resolved before the cd, not climbed back up to afterwards: Concourse
            # lays outputs out as siblings of the task's working directory, while
            # the specs have to run from inside the checkout. The start time is
            # what keeps one failure's artifacts from overwriting the last one's,
            # since no build number is reachable from here; match it to a build by
            # the build's start time in Concourse.
            f'artifact_dir="$PWD/{ARTIFACT_OUTPUT}/{artifact_run_prefix}/$run_stamp"',
            f'report_dir="$PWD/{REPORT_OUTPUT}/{artifact_run_prefix}"',
            *verdict_root_lines,
            f"cd canary-code/{CANARY_REPO_PATH}",
            # @playwright/test is not installed globally in the image, so this is
            # mandatory. It is also cheap -- 6 packages, no browser download,
            # because the browsers are already in the image -- which is why no
            # bespoke canary image is built. Keep it that way (see AGENTS.md).
            "npm ci",
            # The artifacts worth having exist only when the run fails, which is
            # precisely when a non-zero exit under `set -e` would skip collecting
            # them. So the status is captured and re-raised at the end instead.
            "set +e",
            f"npx playwright test {spec_arguments} {project_flags}",
            "canary_status=$?",
            "set -e",
            'mkdir -p "$artifact_dir" "$report_dir"',
            # Absent when the failure came before any test ran -- a bad image, a
            # failed npm ci -- in which case there is nothing to publish and the
            # exit status below is the whole report. Both uploads tolerate that:
            # Concourse creates a declared output as an empty directory, and an
            # rclone copy from an empty directory is a no-op that exits 0.
            "if [ -d canary-results ]; then",
            '  cp -R canary-results/. "$artifact_dir"/',
            "fi",
            # Flat, one object per run named for the run, rather than a directory
            # per run: it makes the whole flake history a single `aws s3 ls` whose
            # output is one sortable line per run.
            "if [ -f canary-results/results.json ]; then",
            '  cp canary-results/results.json "$report_dir/$run_stamp.json"',
            "fi",
            *verdict_lines,
            'exit "$canary_status"',
        ]
    )

    # env_auth: the worker instance role already grants ol-eng-artifacts writes,
    # so no credential is configured here or resolved from Vault.
    artifact_store = Resource(
        name=Identifier("canary-artifacts"),
        type="rclone",
        icon="bucket",
        source={
            "config": textwrap.dedent(
                """\
                [s3-remote]
                type = s3
                provider = AWS
                env_auth = true
                region = us-east-1
                """
            )
        },
    )

    evidence_puts = [
        PutStep(
            put=artifact_store.name,
            no_get=True,
            # `inputs` is not optional here: without it Concourse streams every
            # artifact in the plan, including the failure tree and the
            # repository checkout, into the put container to upload one small
            # file.
            inputs=[output],
            params={
                "source": str(output),
                "destination": [
                    {
                        "command": "copy",
                        "dir": f"s3-remote:{ARTIFACT_BUCKET}/{prefix}/",
                    }
                ],
            },
        )
        for output, prefix in evidence_uploads
    ]

    # The deploy trigger is a second trigger on the *same* job, not a second
    # job. Two jobs would need a serial_group to stop a deploy-triggered run and
    # a scheduled run hitting the property at once, and would split the run
    # history -- including the flake record -- across two places. One job with
    # max_in_flight=1 already queues them, so a deploy landing mid-run runs the
    # canary again straight afterwards rather than interleaving with it.
    #
    # This also means the marker is an input to *every* run, scheduled ones
    # included, so each run names the release it found deployed. That is only
    # safe because the marker resource declares an initial version: a plain
    # `get` on a resource with no versions never becomes schedulable, which
    # would have silently stopped the scheduled runs too.
    trigger_steps: list[GetStep] = [
        GetStep(get=canary_schedule.name, trigger=True),
        GetStep(get=canary_code.name, trigger=True),
    ]
    release_ref_steps: list[LoadVarStep] = []
    if deploy_marker:
        trigger_steps.append(GetStep(get=deploy_marker.name, trigger=True))
        release_ref_steps.append(
            # `version` is written by the s3 resource's `in` and holds the
            # version captured from the object key -- the release calver, or
            # INITIAL_MARKER_VERSION before the app has ever published a marker.
            LoadVarStep(
                load_var="release_ref",
                file=f"{deploy_marker.name}/version",
                reveal=True,
            )
        )

    return Pipeline(
        resource_types=[rclone()],
        resources=[
            canary_code,
            canary_schedule,
            artifact_store,
            *([deploy_marker] if deploy_marker else []),
        ],
        jobs=[
            Job(
                name=canary_job,
                # A canary that piles up behind a slow run reports on a target it
                # is also still loading, and the failures interleave.
                max_in_flight=1,
                plan=[
                    *trigger_steps,
                    *release_ref_steps,
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
                            outputs=[
                                Output(name=ARTIFACT_OUTPUT),
                                *(Output(name=name) for name, _ in evidence_uploads),
                            ],
                            params=task_params,
                            run=Command(
                                path="bash",
                                args=["-c", run_canary],
                            ),
                        ),
                        # Only on failure. A green run's artifacts are a report
                        # nobody reads, and at this cadence uploading them all
                        # costs more storage than the failures it would bury.
                        # `copy` rather than `sync`: sync mirrors deletions, which
                        # against a build-stamped prefix would erase the history
                        # this exists to keep.
                        on_failure=PutStep(
                            put=artifact_store.name,
                            no_get=True,
                            inputs=[ARTIFACT_OUTPUT],
                            params={
                                "source": str(ARTIFACT_OUTPUT),
                                "destination": [
                                    {
                                        "command": "copy",
                                        "dir": (
                                            f"s3-remote:{ARTIFACT_BUCKET}"
                                            f"/{ARTIFACT_PREFIX}/"
                                        ),
                                    }
                                ],
                            },
                        ),
                        # Every run, green included -- unlike the tree above.
                        # This is ~6-20KB of JSON against the ~19MB a failure
                        # tree costs, and it is the only surviving record that a
                        # green run had to retry a journey to get there.
                        #
                        # `ensure` rather than a following step: a step after the
                        # task would be skipped on exactly the red builds whose
                        # record is most worth having. It runs alongside
                        # on_failure on a red build, which duplicates this ~20KB
                        # into the failure tree as well -- cheap, and it keeps the
                        # run history uniform across green and red.
                        #
                        #
                        # Wrapped in `try` because Concourse propagates a hook
                        # failure to its parent: "If the parent step succeeds
                        # and the ensured step fails, the overall step fails."
                        # Unwrapped, a transient rclone/S3/IAM failure uploading
                        # this file would turn a canary that passed every
                        # journey red -- the target would be fine and the build
                        # would say it was not. This file is evidence, and
                        # evidence must never be able to contradict the result
                        # it is evidence about. Losing one run's record is the
                        # cheaper failure by a wide margin.
                        #
                        # The on_failure put above is deliberately NOT wrapped:
                        # it only runs on builds that are already red, so it
                        # cannot change an outcome, and leaving it bare keeps a
                        # broken artifact upload visible instead of silent.
                        #
                        # The verdict upload rides in the same hook, in
                        # parallel and under the same `try`. `in_parallel` does
                        # not fail fast, so neither upload can stop the other
                        # from being attempted, and the single `try` keeps the
                        # "evidence cannot contradict the result" rule intact
                        # for both. A canary with no deploy trigger has no
                        # verdict to upload and renders one put here, as it did
                        # before verdicts existed.
                        #
                        # A verdict that fails to upload after a *green* run
                        # leaves the release with no pass recorded, which blocks
                        # its promotion until the next run writes one or someone
                        # breaks glass. That is the direction to fail in: the
                        # alternative is a gate that opens because its evidence
                        # went missing.
                        ensure=TryStep(try_=InParallelStep(in_parallel=evidence_puts)),
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
