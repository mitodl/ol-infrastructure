"""Scheduled out-of-band maintenance for the omnigraph graph store.

``optimize`` (Lance fragment compaction) and ``cleanup`` (old-version GC) are
DIRECT-STORAGE commands: they reject ``--server`` outright and operate on the
S3 store behind the running server's back. That is by design — the server's
in-process publisher CAS lets optimize rebase-and-retry against concurrent
writers, so this is safe to run while the data tier is serving, and neither
command needs (or can use) the HTTP API.

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
# `--keep <N>` and `--older-than <duration>` can both be passed, but the CLI
# only reports the combined policy ("keep 2 versions, remove anything older
# than 2592000s") without stating whether it intersects or unions them. An
# intersection is more conservative than either alone; a union would let
# `--keep` delete versions younger than the age cutoff. Since the difference is
# unverified upstream and only one of the two readings is safe, this passes
# `--older-than` ALONE, which is safe under both: nothing younger than the
# cutoff can be removed no matter how the flags combine.
#
# 30d is chosen against what actually needs the history — witan-code's
# per-writer WIP branch views, which are per-session/per-git-branch and
# measured in days — plus a wide margin for time-travel reads over the recent
# past. The cost of the generous window is that a heavily-written graph carries
# up to 30 days of dead versions; that is bounded storage, and fragment bloat
# (the thing that actually degrades query latency) is handled nightly by
# optimize, not by this.
DEFAULT_CLEANUP_OLDER_THAN = "30d"

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
    """
    graph_list = " ".join(shlex.quote(graph) for graph in graph_ids)
    args = " ".join(shlex.quote(arg) for arg in extra_args)
    return f"""set -u
failed=""
for graph in {graph_list}; do
    echo "=== omnigraph {command} ${{graph}}"
    if omnigraph {command} \\
        --cluster "${{OMNIGRAPH_STORAGE_ROOT}}" \\
        --graph "${{graph}}" \\
        --as "${{OMNIGRAPH_MAINTENANCE_ACTOR}}" {args}; then
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
    maintenance_actor: str,
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
                                        kubernetes.core.v1.EnvVarArgs(
                                            name="OMNIGRAPH_MAINTENANCE_ACTOR",
                                            value=maintenance_actor,
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
    """Handles to the provisioned maintenance CronJobs."""

    optimize: kubernetes.batch.v1.CronJob
    cleanup: kubernetes.batch.v1.CronJob


def create_maintenance(  # noqa: PLR0913
    stack_info: StackInfo,
    namespace: str,
    k8s_global_labels: dict[str, str],
    image: str | Output[str],
    service_account_name: str,
    aws_region: str,
    storage_uri: Output[str],
    maintenance_actor: str,
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
        maintenance_actor=maintenance_actor,
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
        maintenance_actor=maintenance_actor,
        schedule=cleanup_schedule,
        script=_sweep_script(
            "cleanup",
            ["--older-than", cleanup_older_than, "--confirm", "--yes"],
            graph_ids,
        ),
        depends_on=depends_on,
        suspend=suspend,
    )

    return OmnigraphMaintenance(
        optimize=optimize_cron_job,
        cleanup=cleanup_cron_job,
    )
