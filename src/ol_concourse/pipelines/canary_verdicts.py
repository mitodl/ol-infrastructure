"""The cross-pipeline contract for "the canary judged this release".

The companion to :mod:`ol_concourse.pipelines.deploy_markers`, running the other
way. A deploy marker tells a canary *a release is now on RC*; a **canary
verdict** tells the deploy pipeline *what the canary found when it tested that
release*, so a promotion to production can be blocked on it (issue #5592,
"failures should block releases from going to production").

A verdict is a small JSON object written to S3 by the canary run, under a key
whose directory segment is the release version::

    s3://ol-eng-artifacts/canary-verdicts/<app>/<environment>/<version>/pass.json
    s3://ol-eng-artifacts/canary-verdicts/<app>/<environment>/<version>/fail.json

**The version is in the key.** That is the whole point, and it is the property
the gating design turns on: the gate asks "did the canary pass *the release I am
about to promote*", which is one `head-object` on a key it can compute from
``((.:image_tag))``. The failure mode being designed against is a gate that
consults the most recent canary run of any kind and is satisfied by a green run
that tested the *previous* release. A verdict that could only be found by
scanning a time-ordered history would reintroduce exactly that.

Neither side uses a Concourse resource for this, unlike the deploy marker:

- The **producer** is the canary task itself, not a `put`. The verdict has to be
  written on both the green and the red path, and a `put` step cannot run on the
  red path without also being able to redden a green build (see the ``ensure``
  reasoning in ``canaries/pipeline.py``). The canary script writes the file and
  the existing rclone `put` ships it.
- The **consumer** is a task in the production deploy job that checks for one
  specific key. A Concourse `get` resolves the *latest* version of a resource,
  which is the stale-green failure mode above; there is no way to pin a `get` to
  a build-time var.

So what this module owns is the key layout and the break-glass command, and both
ends build them from here rather than spelling them out. A producer writing keys
the consumer does not look for is a gate that blocks every promotion until
someone breaks glass -- or, worse the other way round, one that passes because
it is looking at a key nothing ever writes.

Current participants:

- Producer: ``ol_concourse.pipelines.canaries.pipeline``, for any canary with a
  ``deploy_trigger`` (no trigger means no release identity, so nothing to key a
  verdict on).
- Consumer: ``ol_concourse.pipelines.infrastructure.k8s_apps.pipeline``, opt-in
  per app via ``AppPipelineParams.gate_production_on_rc_canary``.

Both meta pipelines watch this file, so a change to the layout re-renders both
sides together. If you add a third participant, add its meta pipeline's watch
list too.
"""

from ol_concourse.pipelines.deploy_markers import (
    DEPLOY_MARKER_BUCKET,
    INITIAL_MARKER_VERSION,
)

# The same bucket the deploy markers live in, imported rather than respelled so
# the two halves of the round trip cannot end up in different buckets through a
# typo. It is already covered by the operations Concourse workers' instance role
# (Get/Put/List, see applications/concourse/iam_policies/operations.py), which is
# why neither the producer nor the gate configures a credential.
CANARY_VERDICT_BUCKET = DEPLOY_MARKER_BUCKET
CANARY_VERDICT_PREFIX = "canary-verdicts"

# Object basenames within a version's directory. Fixed names rather than
# per-run stamps: the gate is a key existence check, and a stamped name would
# make it a listing plus a parse. A second green run for the same release
# overwrites pass.json, which loses nothing -- the per-run history lives in
# canary-runs/ and is keyed by time precisely so this does not have to be.
# The S105 suppression is a false positive: this is half of an object basename,
# not a password.
PASS_VERDICT = "pass"  # noqa: S105  # pragma: allowlist secret
FAIL_VERDICT = "fail"

# Written by a human to authorise one promotion past a canary that has not
# passed. Deliberately per-version, so it cannot outlive the incident it was
# written for: the next release needs a new one.
BREAK_GLASS_VERDICT = "break-glass"

# A verdict is only meaningful for a release the canary can name. These are the
# two values ``CANARY_RELEASE_REF`` takes when it cannot: ``unknown`` for a
# canary with no deploy trigger at all, and the deploy marker's synthetic
# initial version before the app has ever published one. A verdict written under
# either would be a claim about "the release called 0.0.0.0", which nothing can
# promote.
UNATTRIBUTABLE_RELEASE_REFS = ("", "unknown", INITIAL_MARKER_VERSION)


def canary_verdict_directory(app_name: str, environment: str, version: str) -> str:
    """Return the bucket-relative directory holding one release's verdicts.

    :param app_name: Application name as ``k8s_apps`` knows it (e.g.
        ``mit-learn``). Matches the deploy marker's app segment, because the
        release identity a verdict carries came from that marker.
    :param environment: Environment the canary tested, e.g.
        :data:`~ol_concourse.pipelines.deploy_markers.RC_ENVIRONMENT`.
    :param version: Release version, e.g. ``2026.9.21.1``. May be a shell
        variable reference when rendering a script -- see
        :func:`canary_verdict_key`.
    :returns: A key prefix with no leading or trailing slash.
    """
    return f"{CANARY_VERDICT_PREFIX}/{app_name}/{environment}/{version}"


def canary_verdict_key(
    app_name: str, environment: str, version: str, outcome: str
) -> str:
    """Return the full object key for one verdict.

    Rendered into shell on both ends, so ``version`` is routinely passed as
    ``"$RELEASE_VERSION"`` rather than a literal: the release is only known at
    build time, on the producing side from the deploy marker and on the
    consuming side from ``((.:image_tag))``.

    :param app_name: Application name.
    :param environment: Environment name.
    :param version: Release version, or a shell expansion producing one.
    :param outcome: :data:`PASS_VERDICT`, :data:`FAIL_VERDICT` or
        :data:`BREAK_GLASS_VERDICT`.
    :returns: A bucket-relative object key.
    """
    directory = canary_verdict_directory(app_name, environment, version)
    return f"{directory}/{outcome}.json"


def break_glass_command(app_name: str, environment: str, version: str) -> str:
    """Return the exact command that authorises one blocked promotion.

    Printed by the gate when it blocks, and reproduced in
    ``docs/canary-release-gate-break-glass-runbook.md``. Generated from one
    function so the runbook cannot drift from what the gate actually reads --
    an override command that does not work is the same as having no override,
    and it is discovered at the worst possible moment.

    The ``reason`` is not decoration: the gate refuses a break-glass object
    whose reason is missing or empty. An override is a decision someone made,
    and the object is the only place that decision is recorded.

    :param app_name: Application name.
    :param environment: Environment name.
    :param version: Release version, or a shell expansion producing one.
    :returns: A multi-line shell snippet.
    """
    key = canary_verdict_key(app_name, environment, version, BREAK_GLASS_VERDICT)
    return "\n".join(
        [
            f"aws s3 cp - s3://{CANARY_VERDICT_BUCKET}/{key} <<'JSON'",
            '{"reason": "WHY THIS MUST SHIP WITH A FAILING CANARY",',
            ' "who": "your-kerb", "when": "YYYY-MM-DDTHH:MM:SSZ"}',
            "JSON",
        ]
    )
