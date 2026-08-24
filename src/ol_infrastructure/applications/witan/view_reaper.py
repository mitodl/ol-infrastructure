"""The stale-view reaper: the only process ever entitled to delete a WIP view.

Every developer's every git branch gets its own view on a shared code graph
(agent-kit ADR-0006 `witan-code branches`), and nothing about indexing a
branch ever unindexes it. Cedar grants `branch_delete` on unprotected
branches to `witan-ci` alone — a user can create a view and cannot remove it,
not even their own, because Cedar cannot scope a delete to the view's writer
any more than it can scope the write (ADR-0006 D3). `witan code reap-views`
is the client half of that: it deletes views nobody has written in
`WITAN_CODE_VIEW_MAX_IDLE_DAYS` (default 14) and refuses to delete from a
shared graph unless `WITAN_CODE_INDEX_ROLE=ci`. Without a scheduled runner of
that command, the Cedar grant existing is not enough — nothing ever calls it,
so branch sprawl accumulates without bound (ADR-0006 "Consequences": "ol-
infrastructure owns scheduling it").

WHY THIS IS SEPARATE FROM THE CI INDEXER

`create_ci_indexer` writes each repo's `main` view from a git checkout, which
is why it needs a scratch volume, a GitHub App credential, and a repo list.
Reaping touches no git remote and writes nothing new — it reads each graph's
branch ages from the commit log and deletes what has gone idle — so none of
that machinery applies. It shares only the identity: same `witan-ci-token`
secret, same `WITAN_CODE_TOKEN` / `WITAN_CODE_SERVER` / direct-transport
wiring, because Cedar's `branch_delete` grant and the indexer's `main`-write
grant are both scoped to the same `witan-ci` group.

WHY NO REPO LIST

`witan code reap-views` with no `--store` sweeps every graph the server's own
registry reports (`per_repo_stores` -> `safe_cluster_graphs`) plus the bridge,
not a caller-supplied list — so unlike the indexer this job does not need
`managed_repos` threaded in, and a graph provisioned after this stack last
deployed is swept without a config change here.
"""

import pulumi_kubernetes as kubernetes
from pulumi import Output, Resource, ResourceOptions

from ol_infrastructure.applications.witan.observability import (
    downward_api_env_args,
    otel_env,
    witan_log_env,
)
from ol_infrastructure.lib.pulumi_helper import StackInfo

# Default cadence. Reaping is idempotent and cheap relative to the indexer
# (no clone, no parse — a commit-log read and, rarely, a delete per graph), so
# daily is chosen for freshness of the sprawl bound rather than out of
# necessity. Offset from the omnigraph optimize/cleanup CronJobs' 03:20/04:20
# schedules (applications/omnigraph/maintenance.py) so the three maintenance
# sweeps don't contend for the same minute. Overridable per environment via
# `witan:reap_views_schedule`.
DEFAULT_REAP_VIEWS_SCHEDULE = "50 4 * * *"

# A sweep is one commit-log read per graph plus, rarely, a delete — nothing
# like the indexer's full-repo clone-and-parse. Ten minutes is generous
# headroom, not a measured ceiling.
REAP_VIEWS_ACTIVE_DEADLINE_SECONDS = 10 * 60

# One retry covers the transient case (an omnigraph-server rollout mid-sweep)
# without masking a real failure for a full extra interval.
REAP_VIEWS_BACKOFF_LIMIT = 1


def create_view_reaper(  # noqa: PLR0913
    stack_info: StackInfo,
    namespace: str,
    k8s_global_labels: dict[str, str],
    witan_image: str | Output[str],
    omnigraph_server_addr: str | Output[str],
    schedule: str,
    witan_ci_token_secret_name: str,
    witan_ci_token_secret_key: str,
    witan_ci_token_secret: Resource,
    service_version: str,
) -> kubernetes.batch.v1.CronJob:
    """Provision the CronJob that deletes idle branch views on every code graph.

    Runs `witan code reap-views --apply` under the `witan-ci` identity Cedar's
    `ci-manage-wip-branches` rule grants `branch_delete` to — the same
    identity and secret the CI indexer uses, since both grants are scoped to
    the same group. `main` and any view with no commits of its own are never
    reaped (ADR-0006 D5); everything else idle past
    `WITAN_CODE_VIEW_MAX_IDLE_DAYS` (server-side default 14 days) is deleted.
    """
    reaper_env = [
        # Addressed directly, same as the CI indexer and for the same reason:
        # reaping reads commit logs and deletes branches, neither of which the
        # MCP tier serves (`witan code reap-views` refuses outright when
        # `code_transport == mcp`, see witan_code/cli.py).
        kubernetes.core.v1.EnvVarArgs(
            name="WITAN_CODE_SERVER", value=omnigraph_server_addr
        ),
        kubernetes.core.v1.EnvVarArgs(name="WITAN_CODE_TRANSPORT", value="direct"),
        kubernetes.core.v1.EnvVarArgs(
            name="WITAN_CODE_TOKEN",
            value_from=kubernetes.core.v1.EnvVarSourceArgs(
                secret_key_ref=kubernetes.core.v1.SecretKeySelectorArgs(
                    name=witan_ci_token_secret_name,
                    key=witan_ci_token_secret_key,
                )
            ),
        ),
        # Declared explicitly, unlike the CI indexer: that job's entrypoint
        # script (`witan-ci-index.sh`) asserts the role itself so it cannot be
        # forgotten by a deployment. This job invokes `witan code reap-views`
        # directly with no wrapper script, so the deployment is the only place
        # left to declare it — and reap-views refuses to delete from a shared
        # graph without it (agent-kit `witan_code/cli.py` `reap_views`).
        kubernetes.core.v1.EnvVarArgs(name="WITAN_CODE_INDEX_ROLE", value="ci"),
    ]
    reaper_env += [
        kubernetes.core.v1.EnvVarArgs(name=name, value=value)
        for name, value in (
            witan_log_env()
            | otel_env(stack_info, "witan-code-view-reaper", service_version)
        ).items()
    ]
    reaper_env += downward_api_env_args()

    return kubernetes.batch.v1.CronJob(
        f"witan-view-reaper-{stack_info.env_suffix}",
        metadata=kubernetes.meta.v1.ObjectMetaArgs(
            name="witan-view-reaper",
            namespace=namespace,
            labels=k8s_global_labels,
        ),
        spec=kubernetes.batch.v1.CronJobSpecArgs(
            schedule=schedule,
            # Two sweeps of the same graph set concurrently is harmless on its
            # own (reaping is idempotent — a view already gone is not an
            # error) but forbidden anyway so overlapping runs cannot both log
            # against the same window and double the delete traffic for no
            # reason.
            concurrency_policy="Forbid",
            starting_deadline_seconds=600,
            successful_jobs_history_limit=1,
            failed_jobs_history_limit=3,
            job_template=kubernetes.batch.v1.JobTemplateSpecArgs(
                metadata=kubernetes.meta.v1.ObjectMetaArgs(
                    labels=k8s_global_labels,
                ),
                spec=kubernetes.batch.v1.JobSpecArgs(
                    backoff_limit=REAP_VIEWS_BACKOFF_LIMIT,
                    active_deadline_seconds=REAP_VIEWS_ACTIVE_DEADLINE_SECONDS,
                    template=kubernetes.core.v1.PodTemplateSpecArgs(
                        metadata=kubernetes.meta.v1.ObjectMetaArgs(
                            labels={
                                **k8s_global_labels,
                                "app.kubernetes.io/name": "witan-view-reaper",
                            },
                        ),
                        spec=kubernetes.core.v1.PodSpecArgs(
                            restart_policy="Never",
                            # Talks to omnigraph-server only, never the
                            # Kubernetes API — same reasoning as the CI
                            # indexer.
                            automount_service_account_token=False,
                            containers=[
                                kubernetes.core.v1.ContainerArgs(
                                    name="witan-view-reaper",
                                    image=witan_image,
                                    # The image's `witan` ENTRYPOINT is kept
                                    # (unlike the CI indexer, which overrides
                                    # it): this runs the mounted `code
                                    # reap-views` subcommand, not a standalone
                                    # sweep script.
                                    args=["code", "reap-views", "--apply"],
                                    env=reaper_env,
                                    resources=kubernetes.core.v1.ResourceRequirementsArgs(
                                        requests={"cpu": "100m", "memory": "128Mi"},
                                        limits={"cpu": "500m", "memory": "512Mi"},
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
