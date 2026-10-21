"""Scheduled out-of-band maintenance for the omnigraph graph store.

TWO MODELS, CHOSEN BY THE SERVED STORAGE FORMAT

Up to format 9 (omnigraph 0.11) the two sweeps below run beside the serving
data tier, and most of this docstring is about that. From format 14 (0.12 and
later) every writer of a cluster root keeps an exclusive lock, so `optimize`
and `cleanup` are refused with `state_lock_held` while the server is up. There
one CronJob owns a nightly stop window instead: see
``create_maintenance_window`` at the end of this module. Everything between
here and there describes the pre-14 sweeps and goes away with the last
environment on format 9.

``optimize`` (Lance fragment compaction) and ``cleanup`` (old-version GC) are
DIRECT-STORAGE commands: they reject ``--server`` outright and operate on the
S3 store behind the running server's back. That is by design — the server's
in-process publisher CAS lets optimize rebase-and-retry against concurrent
writers, so this is safe to run while the data tier is serving, and neither
command needs (or can use) the HTTP API.

★ THAT SAFETY CLAIM IS NARROWER THAN IT READS, AND WE HAVE COUNTEREXAMPLES.
The CAS above is about concurrent WRITERS, and nothing observed contradicts it.
It says nothing about the RECOVERY BARRIER, and there the interaction with a
long-lived server is not benign: on four days between 2026-08-15 and 08-25 the
first write to arrive after this job compacted ``code-bridge`` left a pending
Mutation recovery operation on branch ``main`` that the running server could
not clear. Every subsequent bridge write failed with

    recovery required for operation <ULID>: pending Mutation recovery operation
    on branch 'main' blocks the synchronous write/control recovery barrier
    (datasets dataset for node type 'RepoSymbol'); reopen the graph read-write
    before retrying

for as long as the server stayed up — 15 hours on 08-25, ending only when it
was restarted. The evidence that this job is the trigger rather than a
coincidence: the operation ids are ULIDs, and all four decode to 04:01:3x UTC,
within five seconds of each other, on four different days — the first CI
indexer run (04:00, every 4h) after this job's 03:20 slot. The dataset named in
the barrier, ``RepoSymbol``, is one this job compacts (``frags 14 → 1``).

NOT fully understood: the same compaction runs daily and the wedge appeared on
four days out of fourteen, so compaction alone is not sufficient; the additional
condition that triggers the wedge is unknown.
Server uptime across the optimize run is not the discriminator either — it was
identical on clean days.

So: do NOT read the paragraph above as "compaction while serving is free". It
is free for write conflicts. A cure requires reopening the graph, which today
means restarting omnigraph-server. Tracked in
``tk-production-code-bridge-graph-is-wedged-on-a-pend-8318a4``; filed upstream
because if this is what it looks like it affects every deployment following the
documented maintenance pattern, not just ours.

Deliberately NOT worked around here: adding a post-optimize rollout restart to
this file would make it claim safety while scheduling a daily outage to
compensate, and would need the maintenance ServiceAccount — IRSA for S3, with
no Kubernetes RBAC — granted permission to restart the data tier. If that
workaround is wanted before an upstream fix, it belongs in its own CronJob with
its own narrowly-scoped role, where the privilege is visible.

WHY TWO CRONJOBS AND NOT ONE

They have genuinely different cadences and different risk. ``optimize`` is
non-destructive and wants to run often enough that fragment count never gets
far from steady state; ``cleanup`` permanently destroys old Lance versions and
only needs to run often enough to keep the version history from growing without
bound. Folding them into one job would either run the destructive one nightly
or the cheap one weekly. Their schedules are offset so the two never overlap:
each is ``concurrencyPolicy: Forbid`` against itself, but Kubernetes has no way
to express "Forbid against that other CronJob", so the separation is the
schedule's job.

WHY A LOOP OVER GRAPH IDS RATHER THAN ONE COMMAND

Verified against the omnigraph 0.8.1 CLI: maintenance addressing is
``--cluster <storage-root-URI> --graph <id>``, and it is strictly per-graph.

  - Omitting ``--graph`` is a hard error, not an all-graphs default:
    ``cluster '<uri>' has 2 graphs: [alpha, beta]; pass --graph <id> to select
    one``.
  - Passing the *config directory* as ``--cluster`` fails with ``has no applied
    state`` — the cluster state ledger lives under the storage root
    (``__cluster/state.json``), not next to cluster.yaml, so the storage-root
    URI is the form that works from a pod.
  - A bare positional storage-root URI (``omnigraph optimize s3://bucket``)
    addresses a SINGLE graph store and errors on the cluster root. It is not
    the invocation to use, despite reading like it.

So the graph list has to come from somewhere, and it comes from the same
``build_cluster_graphs`` call that declares the graphs in cluster.yaml — one
source for "which graphs exist", so adding a managed repo extends the
maintenance sweep in the same deploy that creates the graph.

WHY THE LOOP DOES NOT STOP ON THE FIRST FAILURE

With one ``code-<repo>`` graph per managed repo, a single unopenable graph
would otherwise skip every graph after it in the list — silently, since the
failure is one non-zero exit among many. The loop records the failure, keeps
going, and exits non-zero at the end, so a bad graph costs that graph's
maintenance and nothing else while still failing the Job loudly.

``omnigraph repair`` is deliberately NOT scheduled here. It reconciles
manifest/head drift and its ``--force`` mode publishes drift a human has not
verified; it is a reactive, operator-driven command. See the runbook.
"""

import shlex
from typing import NamedTuple

import pulumi_kubernetes as kubernetes
from pulumi import Output, Resource, ResourceOptions

from ol_infrastructure.applications.omnigraph.lock_handoff import UNLOCK_LEASE_NAME
from ol_infrastructure.lib.pulumi_helper import StackInfo

# Nightly compaction, and weekly version GC an hour later on Sunday. Both are
# in the cluster's UTC, and both sit in the small hours to keep their S3 read
# amplification away from the CI indexer's own bursts.
#
# The one-hour gap is what keeps cleanup from running while optimize is still
# going: optimize rewrites fragments and cleanup deletes old versions, and
# running them at once means cleanup racing the versions optimize is in the
# middle of creating. An hour is far beyond a run's expected duration at this
# store size (see ACTIVE_DEADLINE_SECONDS) while still being auditable as "the
# same night".
DEFAULT_OPTIMIZE_SCHEDULE = "20 3 * * *"
DEFAULT_CLEANUP_SCHEDULE = "20 4 * * 0"

# Version retention for `cleanup`, expressed as an age rather than a count.
#
# `--older-than` is passed alone. `--keep <N>` counts graph commits per live
# branch, and the code graphs carry one branch per writer, so a count bounds
# nothing useful here. (The two combine as a union: a commit either one retains
# survives. omnigraph v0.13.0 docs/user/operations/maintenance.md#cleanup.)
#
# The age is sized by how long the stop window takes, not by storage. From
# format 14 cleanup runs with the server stopped, and before it deletes
# anything it opens every table version present, retained or not
# (omnigraph v0.13.0 collector.rs discover_published and plan_sweep, run in
# full by optimize.rs cleanup_detached_only before any sweep). One run costs
# about 0.14s per present version on S3: QA's code-bridge took 488s to remove
# 12 of its 3,522 versions on 2026-10-18. A run stopped during that plan
# deletes nothing. Run nightly (DEFAULT_CLEANUP_ISO_WEEKDAYS) at 2d, each run
# sees about three days of writes. Production's code-bridge writes about 800
# table versions a day (S3 listing of _versions/, 2026-10-17 to 10-19), so
# that is about 2,400 versions, about 6 minutes. A Sunday-only run sees nine
# days at 2d and 37 at 30d.
#
# witan's own code needs no older versions. The current head of every live
# branch is always retained whatever its age, as is a named branch's oldest
# commit, so witan-code's per-writer WIP branch views do not depend on this,
# and witan makes no time-travel reads. The cost is the operator's undelete:
# a hard-deleted witan row stays readable through `omnigraph query --snapshot`
# only while the commit before the delete is retained, so 2 days instead of
# 30. Past that, and for a bad cleanup run, the restore is the bucket's
# noncurrent object versions (data_tier.py, 30 days) or the runbooks'
# `aws s3 sync` backups.
DEFAULT_CLEANUP_OLDER_THAN = "2d"

# Both jobs are S3-bound, single-threaded per graph, and run over a store whose
# graphs are small. An hour without finishing means a wedged S3 connection or a
# held lock, not slow progress — and the deadline has to stay well under the
# gap between the two schedules so a hung optimize cannot still be running when
# cleanup starts.
ACTIVE_DEADLINE_SECONDS = 2700

# No retry. Both commands are idempotent, so a retry would be safe, but neither
# is urgent: the next scheduled run is the retry, and an immediate re-attempt
# of a run that just failed on a held lock or an unopenable graph fails the
# same way while making the failure harder to read in the Job history.
BACKOFF_LIMIT = 0


def _sweep_script(command: str, extra_args: list[str], graph_ids: list[str]) -> str:
    """Render the per-graph sweep both CronJobs run.

    ``graph_ids`` is interpolated as a literal shell word list rather than
    passed through the environment: the ids are Pulumi-time constants derived
    from cluster.yaml's own graph list, and having them visible in the pod spec
    makes ``kubectl get cronjob -o yaml`` show exactly which graphs a run
    covers.

    Nothing here is ``--quiet``: the one-line resolved-target diagnostic each
    command echoes ("omnigraph cleanup -> s3://.../graphs/council.omni") is the
    log line that proves a run addressed the store it was meant to, which is
    the first thing to check when a sweep reports success against nothing.

    Every interpolated value is ``shlex.quote``d. ``extra_args`` carries
    ``cleanup_older_than``, which is Pulumi config an operator can set to
    anything: unquoted, a plausible typo like ``30 days`` silently becomes two
    shell words and the CLI gets a stray positional argument, which is a
    confusing 4am failure rather than an obvious one. Quoted, the same typo
    reaches the CLI as one argument and comes back as a duration parse error
    naming the value. ``graph_ids`` are already normalized to ``[a-z0-9-]`` by
    ``code_graph_id``, so quoting them is a no-op today and stays correct if
    that normalization ever loosens — quoting each id individually preserves
    the word list the ``for`` loop needs.

    NO ``--as``. Both sweeps are storage-native (direct-engine) commands, and
    omnigraph 0.9.0 rejects an actor on those outright::

        `optimize` is a direct (storage-native) command; --as sets the actor
        for a direct-engine or actor-bound cluster operation and does not
        apply. Pass a storage URI, or --cluster <dir> --graph <id>.

    0.8.x accepted and ignored the flag, so this only became visible when the
    0.9.0 image rolled — every optimize run then failed against all 16 graphs.
    Addressing (``--cluster`` + ``--graph``) was never the problem and is
    unchanged. Verified against both binaries before removing it: 0.9.0 without
    ``--as`` compacts, and 0.8.1 without ``--as`` also compacts, so the sweeps
    work on either side of the upgrade.
    """
    graph_list = " ".join(shlex.quote(graph) for graph in graph_ids)
    args = " ".join(shlex.quote(arg) for arg in extra_args)
    return f"""set -u
failed=""
for graph in {graph_list}; do
    echo "=== omnigraph {command} ${{graph}}"
    if omnigraph {command} \\
        --cluster "${{OMNIGRAPH_STORAGE_ROOT}}" \\
        --graph "${{graph}}" {args}; then
        :
    else
        echo "!!! omnigraph {command} failed for ${{graph}}" >&2
        failed="${{failed}} ${{graph}}"
    fi
done
if [ -n "${{failed}}" ]; then
    echo "!!! omnigraph {command} failed for:${{failed}}" >&2
    exit 1
fi
echo "omnigraph {command}: all graphs completed"
"""


def _cron_job(  # noqa: PLR0913
    name: str,
    stack_info: StackInfo,
    namespace: str,
    k8s_global_labels: dict[str, str],
    image: str | Output[str],
    service_account_name: str,
    aws_region: str,
    storage_uri: Output[str],
    schedule: str,
    script: str,
    depends_on: list[Resource],
    *,
    suspend: bool = False,
) -> kubernetes.batch.v1.CronJob:
    """Build one maintenance CronJob around ``script``."""
    return kubernetes.batch.v1.CronJob(
        f"omnigraph-{name}-{stack_info.env_suffix}",
        metadata=kubernetes.meta.v1.ObjectMetaArgs(
            name=f"omnigraph-{name}",
            namespace=namespace,
            labels=k8s_global_labels,
        ),
        spec=kubernetes.batch.v1.CronJobSpecArgs(
            schedule=schedule,
            # Held off entirely while a storage-format migration is armed.
            # Both sweeps write DIRECTLY to the store, bypassing the server, so
            # scaling the Deployment to zero does not stop them: `optimize`
            # rewrites Lance fragments on a root the migration has declared
            # frozen, and would do it between the export and the verification.
            # The runbook's manual form only *checks* for a run in flight,
            # which cannot stop one that starts a minute later.
            #
            # Declared here rather than left to `kubectl patch` because a
            # migration can span days, and any unrelated `pulumi up` in that
            # window would quietly reconcile a hand-patched CronJob back to
            # running.
            suspend=suspend,
            # Two concurrent runs of the same command would contend on the same
            # per-graph storage lock, and the loser would fail the whole sweep
            # after doing real work. Skipping a tick because the previous one is
            # still going is the right answer for maintenance that has no
            # deadline.
            concurrency_policy="Forbid",
            starting_deadline_seconds=600,
            successful_jobs_history_limit=1,
            # A failed sweep is what an operator comes looking for, and it can
            # go unnoticed for days on a weekly schedule — keep several.
            failed_jobs_history_limit=3,
            job_template=kubernetes.batch.v1.JobTemplateSpecArgs(
                metadata=kubernetes.meta.v1.ObjectMetaArgs(labels=k8s_global_labels),
                spec=kubernetes.batch.v1.JobSpecArgs(
                    backoff_limit=BACKOFF_LIMIT,
                    active_deadline_seconds=ACTIVE_DEADLINE_SECONDS,
                    template=kubernetes.core.v1.PodTemplateSpecArgs(
                        metadata=kubernetes.meta.v1.ObjectMetaArgs(
                            labels={
                                **k8s_global_labels,
                                "app.kubernetes.io/name": f"omnigraph-{name}",
                            },
                        ),
                        spec=kubernetes.core.v1.PodSpecArgs(
                            restart_policy="Never",
                            # The omnigraph-server IRSA identity, reused rather
                            # than duplicated: this needs exactly the S3 access
                            # the server already has to the same bucket, and a
                            # second role granting the same thing would be one
                            # more place for the bucket policy to drift.
                            service_account_name=service_account_name,
                            containers=[
                                kubernetes.core.v1.ContainerArgs(
                                    name=name,
                                    # Same image as the data tier, which bakes
                                    # the `omnigraph` CLI alongside the server
                                    # binary. Pinned by digest through the
                                    # caller, so maintenance always runs the
                                    # same storage-format version as the server
                                    # writing the store — the strict-single-
                                    # version rule applies to the CLI too.
                                    image=image,
                                    # Overrides the image's server entrypoint.
                                    command=["/bin/sh", "-c", script],
                                    env=[
                                        kubernetes.core.v1.EnvVarArgs(
                                            name="AWS_REGION", value=aws_region
                                        ),
                                        kubernetes.core.v1.EnvVarArgs(
                                            name="OMNIGRAPH_STORAGE_ROOT",
                                            value=storage_uri,
                                        ),
                                    ],
                                    # Compaction rewrites fragments through
                                    # memory, so this is the one job here with a
                                    # real memory floor. Kept below the server's
                                    # own limit: it is the same node pool, and
                                    # maintenance losing an eviction race to the
                                    # serving pod is the correct outcome.
                                    resources=kubernetes.core.v1.ResourceRequirementsArgs(
                                        requests={"cpu": "100m", "memory": "256Mi"},
                                        limits={"cpu": "1", "memory": "1Gi"},
                                    ),
                                )
                            ],
                        ),
                    ),
                ),
            ),
        ),
        opts=ResourceOptions(depends_on=depends_on),
    )


class OmnigraphMaintenance(NamedTuple):
    """Handles to the provisioned maintenance CronJobs.

    Every one is a direct writer of the storage root, which is what the
    storage-format migration Job waits on before it takes a baseline.
    """

    cron_jobs: list[kubernetes.batch.v1.CronJob]


def create_maintenance(  # noqa: PLR0913
    stack_info: StackInfo,
    namespace: str,
    k8s_global_labels: dict[str, str],
    image: str | Output[str],
    service_account_name: str,
    aws_region: str,
    storage_uri: Output[str],
    graph_ids: list[str],
    optimize_schedule: str,
    cleanup_schedule: str,
    cleanup_older_than: str,
    depends_on: list[Resource],
    *,
    suspend: bool = False,
) -> OmnigraphMaintenance:
    """Provision the scheduled optimize and cleanup sweeps.

    ``graph_ids`` must be the ids cluster.yaml declares — pass the keys of the
    same ``build_cluster_graphs`` result the ConfigMap is rendered from, so a
    graph can never exist without being swept or be swept without existing.

    ``suspend`` holds both sweeps off for the duration of a storage-format
    migration. Set together, never individually: they run an hour apart
    precisely so they cannot overlap each other, and suspending one alone would
    leave the other writing to a root the migration needs frozen.
    """
    optimize_cron_job = _cron_job(
        name="optimize",
        stack_info=stack_info,
        namespace=namespace,
        k8s_global_labels=k8s_global_labels,
        image=image,
        service_account_name=service_account_name,
        aws_region=aws_region,
        storage_uri=storage_uri,
        schedule=optimize_schedule,
        # Non-destructive, so no --confirm and no confirmation prompt to skip.
        script=_sweep_script("optimize", [], graph_ids),
        depends_on=depends_on,
        suspend=suspend,
    )

    # `--confirm` arms the destructive run; `--yes` is separately required
    # because an s3:// store is a NON-LOCAL scope, and a non-local destructive
    # write with no TTY refuses rather than prompting (RFC-011 Decision 9). A
    # pod has no TTY, so without --yes every scheduled run would error out
    # having deleted nothing — a silent no-op dressed as a failure.
    cleanup_cron_job = _cron_job(
        name="cleanup",
        stack_info=stack_info,
        namespace=namespace,
        k8s_global_labels=k8s_global_labels,
        image=image,
        service_account_name=service_account_name,
        aws_region=aws_region,
        storage_uri=storage_uri,
        schedule=cleanup_schedule,
        script=_sweep_script(
            "cleanup",
            ["--older-than", cleanup_older_than, "--confirm", "--yes"],
            graph_ids,
        ),
        depends_on=depends_on,
        suspend=suspend,
    )

    return OmnigraphMaintenance(cron_jobs=[optimize_cron_job, cleanup_cron_job])


# ── Format 14 and later: one stop window ─────────────────────────────────────
#
# The command is agent-kit `docker/omnigraph_maintenance.py`, baked into the
# omnigraph-server image. It scales the server Deployment to 0, waits for its
# pods to exit, takes over the `serve` lock, runs `optimize` (and `cleanup` on
# its weekday) per graph, and restores the replica count, also on SIGTERM.
# Design: agent-kit docs/internals/design/omnigraph-0-13-upgrade-spec.md (D5).
# This module supplies the schedule, the identity and the time budget.

MAINTENANCE_SERVICE_ACCOUNT_NAME = "omnigraph-maintenance"
MAINTENANCE_WINDOW_NAME = "omnigraph-maintenance"

# Every witan user loses the data tier for the length of the window, nightly.
# 03:20 UTC is the old optimize slot. The window has to be over before the CI
# indexer's 04:00 run, which is the first writer to arrive afterwards.
DEFAULT_MAINTENANCE_SCHEDULE = DEFAULT_OPTIMIZE_SCHEDULE

# Every night. A cleanup's time grows with the versions written since the last
# one, so a weekly run plans a week of writes on top of the retained ones (see
# DEFAULT_CLEANUP_OLDER_THAN).
DEFAULT_CLEANUP_ISO_WEEKDAYS = (1, 2, 3, 4, 5, 6, 7)

# THE TIME BUDGET, from the 03:20 start:
#
#   03:45  no new command starts            MAINTENANCE_WINDOW_SECONDS
#   03:55  a command still running is stopped   + COMMAND_TIMEOUT_SECONDS
#   03:56  the Job's deadline, as a backstop    WINDOW_ACTIVE_DEADLINE_SECONDS
#   03:58  SIGKILL, if the close hangs          + TERMINATION_GRACE_SECONDS
#
# The Job deadline is above the sum of the two the command enforces itself, so
# in a run that is merely slow the command closes its own window (unlock,
# scale back up) and the deadline never fires. When it does fire the pod gets
# SIGTERM and the grace period to do the same.
#
# Measured on 0.13 in the first cleanup window (2026-10-18, from the Job logs):
# optimize took 50s (QA) and 57s (Production) for all 18 graphs. QA's cleanup
# took 880s. Production's ran out: three graphs hit COMMAND_TIMEOUT_SECONDS and
# four were never reached, on a weekly cleanup at 30d. What a cleanup costs is
# set by how many versions are present, see DEFAULT_CLEANUP_OLDER_THAN. Graphs
# a window did not reach are logged and fail the Job, and nothing is left
# locked. The command optimizes every graph first and starts the cleanup pass
# at a different graph each night, so a short window delays a graph's cleanup.
# That holds only for a graph whose cleanup fits in COMMAND_TIMEOUT_SECONDS:
# one that never does is stopped every night and never cleaned, and its
# backlog grows. Clearing a backlog that large is the runbook's one-off
# cleanup with a longer timeout. If cleanup does not fit, these constants and
# the start time have to move together: an earlier
# `omnigraph:maintenance_schedule` alone adds no time.
MAINTENANCE_WINDOW_SECONDS = 1500
COMMAND_TIMEOUT_SECONDS = 600
WINDOW_ACTIVE_DEADLINE_SECONDS = 2160
WINDOW_STARTING_DEADLINE_SECONDS = 60
# Stop one command (the image allows 30s), unlock, restore the replica count.
# The image documents 120 as what its close needs.
TERMINATION_GRACE_SECONDS = 120
# How much longer than the server's termination grace the window waits for its
# pods to exit: the time the kubelet and the Deployment controller take to act
# on the scale-down. The image's own default of 90 is a 35s grace plus this.
SERVER_STOP_MARGIN_SECONDS = 55

if WINDOW_ACTIVE_DEADLINE_SECONDS <= (
    MAINTENANCE_WINDOW_SECONDS + COMMAND_TIMEOUT_SECONDS
):
    msg = "the Job deadline would fire before the command closes its own window"
    raise AssertionError(msg)


class OmnigraphMaintenanceWindow(NamedTuple):
    """Handles to the stop-window CronJob and its access."""

    cron_job: kubernetes.batch.v1.CronJob
    role_binding: kubernetes.rbac.v1.RoleBinding


def create_maintenance_window(  # noqa: PLR0913
    stack_info: StackInfo,
    namespace: str,
    k8s_global_labels: dict[str, str],
    image: str | Output[str],
    aws_region: str,
    cluster_configmap_name: str,
    cluster_config_dir: str,
    server_deployment_name: str,
    server_termination_grace_seconds: int,
    schedule: str,
    cleanup_older_than: str,
    cleanup_iso_weekdays: tuple[int, ...],
    depends_on: list[Resource],
    *,
    suspend: bool = False,
) -> OmnigraphMaintenanceWindow:
    """Provision the nightly stop-window CronJob and its ServiceAccount's Role.

    The ServiceAccount itself (``MAINTENANCE_SERVICE_ACCOUNT_NAME``) is an IRSA
    one, created with the server's by the stack's auth binding: the job needs
    the same S3 access. It is a separate identity because of what is bound
    here. Granting the scale verbs to the server's ServiceAccount would let the
    server scale its own Deployment.

    :param cluster_configmap_name: the ConfigMap holding ``cluster.yaml``. The
        command reads the storage root and the graph list from it, so a graph
        cannot exist without being maintained.
    :param server_deployment_name: the Deployment the window stops. The
        command reads its pod selector from it, so there is no second copy of
        the selector here to drift.
    :param server_termination_grace_seconds: that Deployment's pod grace. The
        command gives up on a server that has not stopped within it plus
        ``SERVER_STOP_MARGIN_SECONDS``, so a server still draining writes is
        not taken for a stuck one.
    :param cleanup_iso_weekdays: UTC days ``cleanup`` also runs on (1 is
        Monday), rendered as the comma list the image parses.
    :param suspend: hold the window off while a storage-format migration is
        armed. The command also skips on the Deployment's migration-armed
        annotation, which covers a run that started before the suspend landed.
    """
    role = kubernetes.rbac.v1.Role(
        f"omnigraph-maintenance-window-role-{stack_info.env_suffix}",
        metadata=kubernetes.meta.v1.ObjectMetaArgs(
            name=MAINTENANCE_WINDOW_NAME,
            namespace=namespace,
            labels=k8s_global_labels,
        ),
        rules=[
            kubernetes.rbac.v1.PolicyRuleArgs(
                api_groups=["coordination.k8s.io"],
                resources=["leases"],
                resource_names=[UNLOCK_LEASE_NAME],
                verbs=["get", "update"],
            ),
            # RBAC cannot scope `list` to a label selector.
            kubernetes.rbac.v1.PolicyRuleArgs(
                api_groups=[""], resources=["pods"], verbs=["list"]
            ),
            # `get` on the Deployment for its replica count, resourceVersion
            # and the migration-armed annotation. The write goes through the
            # scale subresource only, so this identity cannot change the image,
            # the command or anything else about the server.
            kubernetes.rbac.v1.PolicyRuleArgs(
                api_groups=["apps"],
                resources=["deployments"],
                resource_names=[server_deployment_name],
                verbs=["get"],
            ),
            kubernetes.rbac.v1.PolicyRuleArgs(
                api_groups=["apps"],
                resources=["deployments/scale"],
                resource_names=[server_deployment_name],
                verbs=["get", "update"],
            ),
        ],
    )
    role_binding = kubernetes.rbac.v1.RoleBinding(
        f"omnigraph-maintenance-window-binding-{stack_info.env_suffix}",
        metadata=kubernetes.meta.v1.ObjectMetaArgs(
            name=MAINTENANCE_WINDOW_NAME,
            namespace=namespace,
            labels=k8s_global_labels,
        ),
        role_ref=kubernetes.rbac.v1.RoleRefArgs(
            api_group="rbac.authorization.k8s.io",
            kind="Role",
            name=role.metadata.name,
        ),
        subjects=[
            kubernetes.rbac.v1.SubjectArgs(
                kind="ServiceAccount",
                name=MAINTENANCE_SERVICE_ACCOUNT_NAME,
                namespace=namespace,
            )
        ],
    )
    pod_labels = {
        **k8s_global_labels,
        "app.kubernetes.io/name": MAINTENANCE_WINDOW_NAME,
    }
    cron_job = kubernetes.batch.v1.CronJob(
        f"omnigraph-maintenance-window-{stack_info.env_suffix}",
        metadata=kubernetes.meta.v1.ObjectMetaArgs(
            name=MAINTENANCE_WINDOW_NAME,
            namespace=namespace,
            labels=k8s_global_labels,
        ),
        spec=kubernetes.batch.v1.CronJobSpecArgs(
            schedule=schedule,
            suspend=suspend,
            # A second window opening on a server the first has stopped would
            # find it at 0 replicas and skip, but there is no reason to start
            # one.
            concurrency_policy="Forbid",
            # A late start pushes the whole budget above towards the 04:00
            # indexer, so the slack is only what the budget has left over.
            starting_deadline_seconds=WINDOW_STARTING_DEADLINE_SECONDS,
            successful_jobs_history_limit=1,
            failed_jobs_history_limit=3,
            job_template=kubernetes.batch.v1.JobTemplateSpecArgs(
                metadata=kubernetes.meta.v1.ObjectMetaArgs(labels=k8s_global_labels),
                spec=kubernetes.batch.v1.JobSpecArgs(
                    # No retry: a second attempt would stop the server again.
                    backoff_limit=BACKOFF_LIMIT,
                    active_deadline_seconds=WINDOW_ACTIVE_DEADLINE_SECONDS,
                    template=kubernetes.core.v1.PodTemplateSpecArgs(
                        metadata=kubernetes.meta.v1.ObjectMetaArgs(labels=pod_labels),
                        spec=kubernetes.core.v1.PodSpecArgs(
                            restart_policy="Never",
                            service_account_name=MAINTENANCE_SERVICE_ACCOUNT_NAME,
                            termination_grace_period_seconds=TERMINATION_GRACE_SECONDS,
                            containers=[
                                kubernetes.core.v1.ContainerArgs(
                                    name="maintenance",
                                    # The server's own image, by digest, so the
                                    # CLI is the storage-format version of the
                                    # server whose lock it takes over.
                                    image=image,
                                    command=["/usr/local/bin/omnigraph_maintenance.py"],
                                    env=[
                                        kubernetes.core.v1.EnvVarArgs(
                                            name="AWS_REGION", value=aws_region
                                        ),
                                        kubernetes.core.v1.EnvVarArgs(
                                            name="OMNIGRAPH_CLUSTER_DIR",
                                            value=cluster_config_dir,
                                        ),
                                        kubernetes.core.v1.EnvVarArgs(
                                            name="OMNIGRAPH_SERVER_DEPLOYMENT",
                                            value=server_deployment_name,
                                        ),
                                        kubernetes.core.v1.EnvVarArgs(
                                            name="OMNIGRAPH_UNLOCK_LEASE",
                                            value=UNLOCK_LEASE_NAME,
                                        ),
                                        kubernetes.core.v1.EnvVarArgs(
                                            name="POD_NAME",
                                            value_from=kubernetes.core.v1.EnvVarSourceArgs(
                                                field_ref=kubernetes.core.v1.ObjectFieldSelectorArgs(
                                                    field_path="metadata.name"
                                                )
                                            ),
                                        ),
                                        kubernetes.core.v1.EnvVarArgs(
                                            name="OMNIGRAPH_CLEANUP_OLDER_THAN",
                                            value=cleanup_older_than,
                                        ),
                                        kubernetes.core.v1.EnvVarArgs(
                                            name="OMNIGRAPH_CLEANUP_ISO_WEEKDAY",
                                            value=",".join(
                                                str(day) for day in cleanup_iso_weekdays
                                            ),
                                        ),
                                        kubernetes.core.v1.EnvVarArgs(
                                            name="OMNIGRAPH_WINDOW_SECONDS",
                                            value=str(MAINTENANCE_WINDOW_SECONDS),
                                        ),
                                        kubernetes.core.v1.EnvVarArgs(
                                            name="OMNIGRAPH_COMMAND_TIMEOUT_SECONDS",
                                            value=str(COMMAND_TIMEOUT_SECONDS),
                                        ),
                                        kubernetes.core.v1.EnvVarArgs(
                                            name="OMNIGRAPH_SERVER_STOP_SECONDS",
                                            value=str(
                                                server_termination_grace_seconds
                                                + SERVER_STOP_MARGIN_SECONDS
                                            ),
                                        ),
                                    ],
                                    # As the sweeps before it: compaction
                                    # rewrites fragments through memory.
                                    resources=kubernetes.core.v1.ResourceRequirementsArgs(
                                        requests={"cpu": "100m", "memory": "256Mi"},
                                        limits={"cpu": "1", "memory": "1Gi"},
                                    ),
                                    volume_mounts=[
                                        # sub_path, as on the server: overlay
                                        # only cluster.yaml.
                                        kubernetes.core.v1.VolumeMountArgs(
                                            name="cluster-config",
                                            mount_path=f"{cluster_config_dir}/cluster.yaml",
                                            sub_path="cluster.yaml",
                                            read_only=True,
                                        ),
                                    ],
                                )
                            ],
                            volumes=[
                                kubernetes.core.v1.VolumeArgs(
                                    name="cluster-config",
                                    config_map=kubernetes.core.v1.ConfigMapVolumeSourceArgs(
                                        name=cluster_configmap_name,
                                    ),
                                ),
                            ],
                        ),
                    ),
                ),
            ),
        ),
        opts=ResourceOptions(depends_on=[*depends_on, role_binding]),
    )
    return OmnigraphMaintenanceWindow(cron_job=cron_job, role_binding=role_binding)
