"""Guard the canary -> production-gate round trip against silent drift.

The canary writes a verdict to an S3 key and the ``k8s_apps`` production deploy
job blocks unless it finds one at that key. Nothing at runtime notices when the
two disagree: the canary stays green, the upload succeeds, and the gate blocks
every promotion of every release until somebody breaks glass and then works out
why. There is no red build anywhere in that failure, which is exactly the shape
of bug a rendered-output test catches cheaply.

So these tests re-derive both halves from the rendered pipelines and compare
them, rather than checking either side against a hardcoded string.
"""

import re

import pytest
from ol_concourse.lib.models.pipeline import PutStep, TaskStep, TryStep

from ol_concourse.pipelines.canaries.pipeline import (
    VERDICT_OUTPUT,
    build_canary_pipeline,
)
from ol_concourse.pipelines.canary_verdicts import (
    BREAK_GLASS_VERDICT,
    FAIL_VERDICT,
    PASS_VERDICT,
    UNATTRIBUTABLE_RELEASE_REFS,
    break_glass_command,
    canary_verdict_key,
)
from ol_concourse.pipelines.deploy_markers import RC_ENVIRONMENT
from ol_concourse.pipelines.infrastructure.k8s_apps.pipeline import (
    AppPipelineParams,
    build_app_pipeline,
)
from ol_concourse.pipelines.infrastructure.k8s_apps.pipeline import (
    pipeline_params as app_pipeline_params,
)

# The one app wired end to end today. Both halves of the round trip are opt-in
# per app, and this is the app they are opted in for.
GATED_APP = "mit-learn"
# Shell variable each side holds the release version in. They differ because
# each side learns it a different way -- the canary from the deploy marker it
# was triggered by, the gate from the image tag it is about to deploy -- and
# the whole point of the test below is that the *key* they build is the same.
CANARY_VERSION_VAR = "${CANARY_RELEASE_REF}"
GATE_VERSION_VAR = "${RELEASE_VERSION}"


def _canary_task_script(canary_name: str) -> str:
    """Return the shell the canary job runs."""
    pipeline = build_canary_pipeline(canary_name)
    task_steps = [step for step in pipeline.jobs[0].plan if isinstance(step, TaskStep)]
    assert len(task_steps) == 1, "the canary job should still run exactly one task"
    return task_steps[0].config.run.args[1]


def _verdict_put(canary_name: str) -> PutStep:
    """Return the put step that uploads the verdict."""
    pipeline = build_canary_pipeline(canary_name)
    task_step = next(
        step for step in pipeline.jobs[0].plan if isinstance(step, TaskStep)
    )
    ensure = task_step.ensure
    assert isinstance(ensure, TryStep), (
        "the verdict and report uploads must stay inside a try: a failed upload "
        "must never be able to redden a canary that passed every journey"
    )
    puts = [
        step
        for step in ensure.try_.in_parallel
        if isinstance(step, PutStep)
        and str(VERDICT_OUTPUT.root) in [str(name) for name in (step.inputs or [])]
    ]
    assert len(puts) == 1, "expected exactly one verdict upload"
    return puts[0]


def _canary_verdict_key_template(canary_name: str) -> str:
    """Rebuild the object key the canary writes, prefix and all.

    The canary only ever names the part of the key below the rclone
    destination's prefix, so the key it actually produces exists only as the
    combination of the two. That combination is what the gate has to match, so
    it is what this reassembles.
    """
    script = _canary_task_script(canary_name)
    local_path = re.search(r'verdict_file="\$verdict_root/(?P<path>[^"]+)"', script)
    assert local_path, "the canary script no longer names a verdict file"
    destination = _verdict_put(canary_name).params["destination"][0]["dir"]
    # "s3-remote:<bucket>/<prefix>/" -> "<prefix>/"
    prefix = destination.split(":", 1)[1].split("/", 1)[1]
    return f"{prefix}{local_path.group('path')}"


def _gate_script(app_name: str) -> str:
    """Return the shell the production job's canary gate runs."""
    pipeline = build_app_pipeline(app_name)
    production_job = next(
        job for job in pipeline.jobs if str(job.name).endswith("production")
    )
    gate = next(
        step
        for step in production_job.plan
        if isinstance(step, TaskStep) and str(step.task).endswith("rc-canary-gate")
    )
    return gate.config.run.args[1]


@pytest.mark.parametrize("outcome", [PASS_VERDICT, FAIL_VERDICT])
def test_producer_and_gate_agree_on_the_verdict_key(outcome: str) -> None:
    """The key the canary writes is the key the gate looks for."""
    written = _canary_verdict_key_template(GATED_APP).replace(
        CANARY_VERSION_VAR, GATE_VERSION_VAR
    )
    # The canary renders one file name for both outcomes and picks between them
    # at runtime; the gate names each outcome separately.
    written_for_outcome = written.replace("${verdict}", outcome)
    assert written_for_outcome == canary_verdict_key(
        GATED_APP, RC_ENVIRONMENT, GATE_VERSION_VAR, outcome
    )
    assert f'"{written_for_outcome}"' in _gate_script(GATED_APP)


def test_gate_checks_the_release_it_is_promoting() -> None:
    """The gate keys on this build's image tag, not on the canary's last run.

    A gate satisfied by the most recent canary result of any kind is satisfied
    by a green run against the *previous* release, which is the failure this
    whole round trip exists to remove.
    """
    pipeline = build_app_pipeline(GATED_APP)
    production_job = next(
        job for job in pipeline.jobs if str(job.name).endswith("production")
    )
    gate_index, gate = next(
        (index, step)
        for index, step in enumerate(production_job.plan)
        if isinstance(step, TaskStep) and str(step.task).endswith("rc-canary-gate")
    )
    assert gate.config.params["RELEASE_VERSION"] == "((.:image_tag))"

    # Nothing that deploys or announces a deploy may precede the gate.
    pulumi_index = next(
        index
        for index, step in enumerate(production_job.plan)
        if isinstance(step, PutStep) and str(step.put).startswith("pulumi-")
    )
    assert gate_index < pulumi_index
    assert all(
        not isinstance(step, PutStep) for step in production_job.plan[:gate_index]
    ), "the gate must run before any put in the production job"


def test_gate_without_a_deploy_marker_is_refused() -> None:
    """A gate with no marker blocks every promotion forever; refuse to render it."""
    with pytest.raises(ValueError, match="publish_rc_deploy_marker"):
        AppPipelineParams(app_name="demo", gate_production_on_rc_canary=True)


def test_gated_apps_publish_deploy_markers() -> None:
    """The live registry holds no half-wired app."""
    for app_name, params in app_pipeline_params.items():
        if params.gate_production_on_rc_canary:
            assert params.publish_rc_deploy_marker, (
                f"{app_name} gates production on its canary but publishes no "
                "deploy marker, so the canary can never name the release"
            )


@pytest.mark.parametrize("release_ref", UNATTRIBUTABLE_RELEASE_REFS)
def test_unnameable_releases_record_no_verdict(release_ref: str) -> None:
    """A verdict about a release nothing can promote can only mislead."""
    script = _canary_task_script(GATED_APP)
    case_arm = re.search(
        r"case \"\$CANARY_RELEASE_REF\" in\n(?P<arms>.+?)\n  \*\)", script, re.DOTALL
    )
    assert case_arm, "the canary script no longer guards on the release ref"
    assert f'"{release_ref}"' in case_arm.group("arms")


def test_break_glass_command_writes_the_key_the_gate_reads() -> None:
    """An override that does not work is discovered mid-incident, or never."""
    command = break_glass_command(GATED_APP, RC_ENVIRONMENT, GATE_VERSION_VAR)
    key = canary_verdict_key(
        GATED_APP, RC_ENVIRONMENT, GATE_VERSION_VAR, BREAK_GLASS_VERDICT
    )
    assert key in command
    gate_script = _gate_script(GATED_APP)
    assert f'break_glass_key="{key}"' in gate_script
    # The gate prints it, because a runbook nobody can find during an incident
    # is not an override.
    assert command in gate_script
