"""Pre-deploy witan data migrations.

Ownership split, deliberate: **schema** convergence is the omnigraph stack's
job (``applications/omnigraph/data_tier.py`` runs ``omnigraph cluster apply``
before its Deployment restarts), because the cluster declares the graphs and
bakes their schema files into the omnigraph-server image. This Job covers only
the migrations that are witan's own — data rewrites the graph engine has no
notion of.

Concretely it does NOT run ``witan migrate all``, which would re-apply
``schema.pg`` via ``omnigraph schema apply --server``. That would reach past
the cluster to a cluster-*managed* graph, duplicating work the omnigraph stack
already did and risking divergence from the cluster's own state ledger. The two
backfills are run individually instead:

- ``migrate topics``    — promote every memory ``tag`` to a ``Topic`` node plus
  a ``Tagged`` edge.
- ``migrate repo-keys`` — fold every stored repo key onto its canonical,
  case-folded form (agent-kit #142), so rows written before that fix stop
  dropping out of repo-scoped reads.

Both are idempotent and safe to re-run, which is what makes them acceptable on
every deploy rather than as one-shot runbook steps.

Note on timing: these are historical backfills. Against the freshly-created,
empty graphs this deployment starts with they find nothing and exit 0 — they
only have work to do once real data lands, which is the local-graph-to-remote
migration tracked in agent-kit task
``tk-remote-server-registration-local-remote-user-dat-dc753c``. Wiring the gate
before there is data to lose is the point: the failure mode this prevents is a
backfill that silently never ran.
"""

import pulumi_kubernetes as kubernetes
from pulumi import Output, Resource, ResourceOptions

from ol_infrastructure.lib.pulumi_helper import StackInfo

# One container per migration, executed strictly in order so the Job stops at
# the first failure. `witan migrate all` is deliberately not used — see the
# module docstring.
WITAN_MIGRATIONS: tuple[tuple[str, list[str]], ...] = (
    ("migrate-topics", ["migrate", "topics"]),
    ("migrate-repo-keys", ["migrate", "repo-keys"]),
)

# omnigraph-server is a single-replica `Recreate` Deployment in a *separate*
# stack, so it has a genuine downtime window on every one of its deploys — and
# since its pod template now carries a config hash, that is every deploy, not
# only the ones that change its image. Two stacks deploying at once can
# therefore land this Job inside that window, where it fails to connect.
#
# Kubernetes' own Job backoff is the retry loop for that: attempts are spaced
# 10s, 20s, 40s, … so 6 retries span roughly ten minutes, comfortably longer
# than a single-pod Recreate rollout (image pull + readiness probe). Each
# attempt re-runs the migration from scratch, which is safe because both are
# idempotent.
#
# A cross-stack `depends_on` — the obvious-looking alternative — is not
# available: a StackReference yields the other stack's *outputs*, not resource
# handles, so there is nothing to order against and no way to make one stack's
# deploy wait on another's rollout.
MIGRATION_BACKOFF_LIMIT = 6


def _migration_container(
    name: str,
    args: list[str],
    witan_image: str | Output[str],
    env: list[kubernetes.core.v1.EnvVarArgs],
    resources: kubernetes.core.v1.ResourceRequirementsArgs,
) -> kubernetes.core.v1.ContainerArgs:
    """One migration step. Identical whether it lands in init or main position."""
    return kubernetes.core.v1.ContainerArgs(
        name=name,
        image=witan_image,
        args=args,
        env=env,
        resources=resources,
    )


def create_migration_job(  # noqa: PLR0913
    stack_info: StackInfo,
    namespace: str,
    k8s_global_labels: dict[str, str],
    witan_image: str | Output[str],
    omnigraph_server_addr: str | Output[str],
    council_graph_id: str | Output[str],
    witan_ci_token_secret_name: str,
    witan_ci_token_secret_key: str,
    witan_ci_token_secret: Resource,
) -> kubernetes.batch.v1.Job:
    """Run witan's own data backfills before the MCPServer serves the new image.

    Talks to omnigraph-server over the cluster network with the same
    ``WITAN_MEMORY_*`` wiring the MCPServer uses, authenticating as
    ``svc-witan-ci`` — these are admin/module-level operations that never carry
    a per-user JWT (agent-kit ADR-0005 path b).
    """
    migration_env = [
        kubernetes.core.v1.EnvVarArgs(
            name="WITAN_MEMORY_URI", value=omnigraph_server_addr
        ),
        kubernetes.core.v1.EnvVarArgs(
            name="WITAN_MEMORY_GRAPH", value=council_graph_id
        ),
        kubernetes.core.v1.EnvVarArgs(
            name="WITAN_MEMORY_TOKEN",
            value_from=kubernetes.core.v1.EnvVarSourceArgs(
                secret_key_ref=kubernetes.core.v1.SecretKeySelectorArgs(
                    name=witan_ci_token_secret_name,
                    key=witan_ci_token_secret_key,
                )
            ),
        ),
        # WITAN_REMOTE_URL is deliberately unset: that selects the remote
        # MCP-client path, over which every `migrate_*` is refused as an
        # admin-only operation (witan/remote/proxy.py `_ADMIN_ONLY`). This Job
        # is the in-cluster path those functions are reserved for, talking to
        # omnigraph-server directly.
    ]
    migration_resources = kubernetes.core.v1.ResourceRequirementsArgs(
        requests={"cpu": "100m", "memory": "256Mi"},
        limits={"cpu": "1", "memory": "1Gi"},
    )

    return kubernetes.batch.v1.Job(
        f"witan-migrations-{stack_info.env_suffix}",
        # Unnamed on purpose (Pulumi auto-naming): a Job's pod template is
        # immutable, so a new image means a replacement, and auto-naming lets
        # the new Job be created before the old one goes away.
        metadata=kubernetes.meta.v1.ObjectMetaArgs(
            namespace=namespace,
            labels=k8s_global_labels,
        ),
        spec=kubernetes.batch.v1.JobSpecArgs(
            backoff_limit=MIGRATION_BACKOFF_LIMIT,
            ttl_seconds_after_finished=86400,
            template=kubernetes.core.v1.PodTemplateSpecArgs(
                metadata=kubernetes.meta.v1.ObjectMetaArgs(
                    labels={
                        **k8s_global_labels,
                        "app.kubernetes.io/name": "witan-migrations",
                    },
                ),
                spec=kubernetes.core.v1.PodSpecArgs(
                    restart_policy="Never",
                    # initContainers run to completion in declaration order and
                    # short-circuit the pod on the first failure, which is the
                    # ordering guarantee these migrations want. A Job also needs
                    # at least one non-init container to complete as, so the
                    # *last* migration is that container rather than a
                    # placeholder: every container here does real work, and
                    # nothing depends on a stub binary being present in an image
                    # this repo doesn't build.
                    init_containers=[
                        _migration_container(
                            name, args, witan_image, migration_env, migration_resources
                        )
                        for name, args in WITAN_MIGRATIONS[:-1]
                    ],
                    containers=[
                        _migration_container(
                            *WITAN_MIGRATIONS[-1],
                            witan_image,
                            migration_env,
                            migration_resources,
                        )
                    ],
                ),
            ),
        ),
        opts=ResourceOptions(depends_on=[witan_ci_token_secret]),
    )
