"""The CI code-graph indexer: the one entitled writer of every shared code graph.

Each repo in the omnigraph stack's ``managed_repos`` has a ``code-<repo>``
graph on the cluster, and the default (``main``) view of that graph — the one
every reader falls back to when it has no branch view of its own — has exactly
one writer. witan-code refuses that write, and the stale-file purge that goes
with it, from any process that has not declared ``WITAN_CODE_INDEX_ROLE=ci``
(agent-kit ``witan_code/graph.py`` ``check_writable``). This CronJob is the
process that declares it. Without it the guard holds and the shared view is
simply never updated by anybody.

WHY IN-CLUSTER, AND NOT GITHUB ACTIONS OR CONCOURSE

omnigraph-server is ClusterIP-only and deliberately has no HTTPRoute (DECIDED
2026-08-01, agent-kit ``witan_code/ingest.py``): the witan MCP tier is the one
exposed boundary, and putting a second, unmediated one next to it was the
thing that decision rejected. Everything outside the cluster therefore reaches
a code graph *through* that tier, at one round trip per store operation —
fine for the few-files-changed reindex a developer's branch does, and not fine
for the thousands a full-repo run makes. Concourse workers are outside the pod
network too, so they cannot reach a ClusterIP at all. That leaves a Kubernetes
workload, which is what this is, keeping the direct ``--server/--graph`` path
the volume needs.

WHY A CRON RATHER THAN A MERGE TRIGGER

"On merge to the default branch" is the ideal trigger and there is nothing
in-cluster to receive it — no webhook endpoint exists, and adding one is a
larger piece of exposed surface than this job is worth. A schedule instead
makes staleness a bound rather than an event: the shared view is at most one
interval behind its repo. That is affordable because indexing is incremental
against the *graph* — file content hashes live in the graph, not in the
working tree, so a fresh clone every run still skips every unchanged file and
only the first run for a repo pays full parse cost.

The image is the ``witan`` MCP-tier image, whose ``witan-ci-index`` entrypoint
does the actual sweep (agent-kit ``docker/witan-ci-index.sh``). Same build as
the tier serving these graphs, so the writer and the readers can never be a
release apart in what they put in them.
"""

import pulumi_kubernetes as kubernetes
from pulumi import Output, Resource, ResourceOptions

from ol_infrastructure.lib.pulumi_helper import StackInfo

# Scratch space for the checkouts. An emptyDir rather than the container
# filesystem so the sweep's disk use is bounded and declared, and mounted at
# its parent rather than at the work dir itself: the entrypoint clears its work
# dir with `rm -rf` on start, which fails with EBUSY against a mount point.
SCRATCH_MOUNT_PATH = "/scratch"
SCRATCH_WORKDIR = f"{SCRATCH_MOUNT_PATH}/witan-ci-index"
# One shallow checkout is live at a time (the entrypoint removes each before
# cloning the next), so this bounds the largest single repo, not their sum.
SCRATCH_SIZE_LIMIT = "8Gi"

# Default cadence. Four hours is a compromise between how stale a shared view
# may get and the fact that every run re-clones every repo — the clone, not the
# parse, is the recurring cost, since parsing is skipped for unchanged files.
# Overridable per environment via `witan:ci_index_schedule`.
DEFAULT_INDEX_SCHEDULE = "0 */4 * * *"

# The first run for a repo parses it from scratch, and this sweeps the whole
# fleet serially, so the ceiling has to clear a cold start on every repo at
# once. Three hours keeps it comfortably under the four-hour cadence, so a
# wedged run is reaped before the next one is due rather than suppressing it.
INDEX_ACTIVE_DEADLINE_SECONDS = 3 * 60 * 60

# Not the usual "retry a few times": a failed sweep costs a full re-clone of
# every repo, and the next scheduled run is already the retry. One retry covers
# the transient case (an omnigraph-server rollout, a GitHub blip) without
# spending an hour of cluster time re-running a sweep that is failing for a
# real reason.
INDEX_BACKOFF_LIMIT = 1


def create_ci_indexer(  # noqa: PLR0913
    stack_info: StackInfo,
    namespace: str,
    k8s_global_labels: dict[str, str],
    witan_image: str | Output[str],
    omnigraph_server_addr: str | Output[str],
    managed_repos: list[str],
    schedule: str,
    witan_ci_token_secret_name: str,
    witan_ci_token_secret_key: str,
    witan_ci_token_secret: Resource,
) -> kubernetes.batch.v1.CronJob | None:
    """Provision the CronJob that indexes each repo's default branch.

    Returns ``None`` when ``managed_repos`` is empty — an environment that
    declares no code graphs has nothing for this job to write, and a CronJob
    whose sweep list is empty would fail on the entrypoint's own required-env
    check every interval.
    """
    if not managed_repos:
        return None

    indexer_env = [
        # The data tier, addressed directly. `code_server` being set is what
        # selects the `--server <url> --graph <id>` path in
        # `witan_code.store.store_for_repo`; the graph id per repo is derived
        # client-side by `witan_code.config.graph_id`, which the omnigraph
        # stack's `code_graph_id` mirrors when it declares those graphs.
        kubernetes.core.v1.EnvVarArgs(
            name="WITAN_CODE_SERVER", value=omnigraph_server_addr
        ),
        # Explicit even though it is the default: this value decides whether a
        # write goes straight to the data tier or through the MCP tier, and the
        # whole reason this job runs in-cluster is to take the former.
        kubernetes.core.v1.EnvVarArgs(name="WITAN_CODE_TRANSPORT", value="direct"),
        # Whitespace-separated, which is what the entrypoint iterates. Sourced
        # from the omnigraph stack's own output rather than re-listed here so
        # the set of repos indexed cannot drift from the set of graphs that
        # exist to index into.
        kubernetes.core.v1.EnvVarArgs(
            name="WITAN_CODE_CI_REPOS", value=" ".join(managed_repos)
        ),
        kubernetes.core.v1.EnvVarArgs(
            name="WITAN_CODE_CI_WORKDIR", value=SCRATCH_WORKDIR
        ),
        kubernetes.core.v1.EnvVarArgs(
            name="WITAN_CODE_TOKEN",
            value_from=kubernetes.core.v1.EnvVarSourceArgs(
                secret_key_ref=kubernetes.core.v1.SecretKeySelectorArgs(
                    name=witan_ci_token_secret_name,
                    key=witan_ci_token_secret_key,
                )
            ),
        ),
        # WITAN_CODE_INDEX_ROLE is deliberately NOT set here: the entrypoint
        # asserts it itself, and one declaration of "this is the CI indexer" is
        # better than two that can disagree.
        #
        # WITAN_ACTOR is deliberately unset too. It would only namespace branch
        # views, and this job writes none — it writes the default view, whose
        # authority is the role above. The identity omnigraph-server records is
        # the one its bearer-token map maps WITAN_CODE_TOKEN to, which is
        # svc-witan-ci either way.
    ]

    return kubernetes.batch.v1.CronJob(
        f"witan-ci-indexer-{stack_info.env_suffix}",
        metadata=kubernetes.meta.v1.ObjectMetaArgs(
            name="witan-ci-indexer",
            namespace=namespace,
            labels=k8s_global_labels,
        ),
        spec=kubernetes.batch.v1.CronJobSpecArgs(
            schedule=schedule,
            # A cold first run can outlast an interval, and two sweeps writing
            # the same default views concurrently is exactly the multi-writer
            # case the role guard exists to prevent — the guard authorizes the
            # role, not one process at a time, so it would not catch this.
            concurrency_policy="Forbid",
            # A missed tick (controller restart, a run still holding the Forbid)
            # is not worth catching up on: the next one indexes the same HEAD.
            starting_deadline_seconds=600,
            successful_jobs_history_limit=1,
            # More failures than successes retained on purpose — a failed sweep
            # is the one whose logs somebody needs.
            failed_jobs_history_limit=3,
            job_template=kubernetes.batch.v1.JobTemplateSpecArgs(
                metadata=kubernetes.meta.v1.ObjectMetaArgs(
                    labels=k8s_global_labels,
                ),
                spec=kubernetes.batch.v1.JobSpecArgs(
                    backoff_limit=INDEX_BACKOFF_LIMIT,
                    active_deadline_seconds=INDEX_ACTIVE_DEADLINE_SECONDS,
                    template=kubernetes.core.v1.PodTemplateSpecArgs(
                        metadata=kubernetes.meta.v1.ObjectMetaArgs(
                            labels={
                                **k8s_global_labels,
                                "app.kubernetes.io/name": "witan-ci-indexer",
                            },
                        ),
                        spec=kubernetes.core.v1.PodSpecArgs(
                            restart_policy="Never",
                            # The image runs as uid/gid 1000 (`witan`); an
                            # emptyDir is root-owned without this, and the
                            # clone would fail on a directory it cannot write.
                            security_context=kubernetes.core.v1.PodSecurityContextArgs(
                                fs_group=1000,
                            ),
                            containers=[
                                kubernetes.core.v1.ContainerArgs(
                                    name="witan-ci-index",
                                    image=witan_image,
                                    # Overrides the image's `witan` ENTRYPOINT:
                                    # this is the sweep script, not a witan
                                    # subcommand.
                                    command=["witan-ci-index"],
                                    env=indexer_env,
                                    volume_mounts=[
                                        kubernetes.core.v1.VolumeMountArgs(
                                            name="scratch",
                                            mount_path=SCRATCH_MOUNT_PATH,
                                        )
                                    ],
                                    # Tree-sitter parsing is single-process and
                                    # CPU-bound; the memory ceiling covers the
                                    # largest repo's record batch, which is
                                    # built in memory before the bulk load.
                                    resources=kubernetes.core.v1.ResourceRequirementsArgs(
                                        requests={"cpu": "500m", "memory": "1Gi"},
                                        limits={"cpu": "2", "memory": "4Gi"},
                                    ),
                                )
                            ],
                            volumes=[
                                kubernetes.core.v1.VolumeArgs(
                                    name="scratch",
                                    empty_dir=kubernetes.core.v1.EmptyDirVolumeSourceArgs(
                                        size_limit=SCRATCH_SIZE_LIMIT,
                                    ),
                                )
                            ],
                        ),
                    ),
                ),
            ),
        ),
        opts=ResourceOptions(depends_on=[witan_ci_token_secret]),
    )
