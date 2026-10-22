"""What the omnigraph 0.13 server entrypoint needs to take over the cluster lock.

From omnigraph 0.12 a server keeps an exclusive lock on its storage root
(``<root>/__cluster/lock.json``) after it exits, and the next start is refused
until that exact lock is force-unlocked. This pod restarts on its own (token
sync, config-hash rollouts, liveness kills, evictions), so the image's
entrypoint does the unlock itself, and only after it has shown that the
previous owner has exited:

- it takes a Kubernetes Lease that every process allowed to force-unlock has
  to hold, because upstream's ``force-unlock`` has no conditional delete and
  two concurrent unlockers can remove each other's lock;
- it lists the pods behind the Deployment's selector and waits until no other
  live one exists, because ``Recreate`` orders pods only during a rollout and a
  deleted or evicted pod's replacement starts while the old one may still be
  committing.

Design: agent-kit ``docs/internals/design/omnigraph-0-13-upgrade-spec.md``
(D3, D4). The entrypoint is agent-kit ``docker/omnigraph_server_entrypoint.py``.

Split out of ``data_tier.py`` for the reason ``cluster_config.py`` is: it can be
imported, and so tested, without ``pulumi_aws``.
"""

from typing import NamedTuple

import pulumi_kubernetes as kubernetes
from pulumi import Output, Resource, ResourceOptions

from ol_infrastructure.applications.omnigraph.cluster_config import (
    require_graph_id_list,
)
from ol_infrastructure.lib.pulumi_helper import StackInfo

# The first storage format whose binaries persist the cluster lock. 0.12 and
# 0.13 both write format 14; 0.11 writes 9 and releases its lock on exit, and
# its `force-unlock` fails on a missing lock. `internal_schema_version` moves to
# 14 in the cutover change, so everything keyed on this arrives with the image
# that needs it.
PERSISTED_LOCK_MIN_FORMAT = 14

UNLOCK_LEASE_NAME = "omnigraph-unlock"
LOCK_HANDOFF_ROLE_NAME = "omnigraph-server-lock-handoff"

# Set on the server Deployment while a storage-format migration is armed. The
# maintenance window (agent-kit `docker/omnigraph_maintenance.py`) skips its
# run when it sees it and does not scale the server back up if it appears
# mid-window. Arming sets `replicas: 0` in the same update, and nothing
# reconciles the count afterwards, so without the marker a window that had
# already scaled the server down would restore it under an armed migration.
MIGRATION_ARMED_ANNOTATION = "ol.mit.edu/migration-armed"


def uses_persisted_lock(internal_schema_version: int | None) -> bool:
    """Whether the served storage format is one whose binaries keep their lock."""
    return (
        internal_schema_version is not None
        and internal_schema_version >= PERSISTED_LOCK_MIN_FORMAT
    )


def validate_graph_removal_allowlist(
    allowlist: list[str], declared_graph_ids: list[str]
) -> list[str]:
    """Return ``allowlist``, refusing an entry that names a graph still declared.

    From 0.13, applying a ``cluster.yaml`` that no longer declares a graph
    deletes its storage and history, and the entrypoint refuses such an apply
    unless the graph is listed here. An entry is therefore only meaningful in
    the change that removes the graph. One left behind for a graph that still
    exists would let a later accidental removal through, which is the case the
    guard is for.

    :param allowlist: graph ids from ``omnigraph:graph_removal_allowlist``.
    :param declared_graph_ids: the ids ``cluster.yaml`` declares in this deploy.
    :returns: the allow-list, unchanged.
    :raises ValueError: when an entry is a declared graph, or the value is
        not a list of graph ids.
    """
    require_graph_id_list(allowlist, "graph_removal_allowlist")
    still_declared = sorted(set(allowlist) & set(declared_graph_ids))
    if still_declared:
        msg = (
            f"omnigraph:graph_removal_allowlist names {still_declared}, which "
            "cluster.yaml still declares. The allow-list permits deleting a "
            "graph's storage and history, so an entry belongs only in the "
            "change that removes the graph. Remove the entries, or remove the "
            "graphs."
        )
        raise ValueError(msg)
    return allowlist


def pod_selector_env(pod_selector: dict[str, str]) -> str:
    """Render ``matchLabels`` as the label selector string the image reads."""
    return ",".join(f"{key}={value}" for key, value in sorted(pod_selector.items()))


def plan_env(
    apply_actor: str, graph_removal_allowlist: list[str]
) -> list[kubernetes.core.v1.EnvVarArgs]:
    """Env for the image's plan check, shared by the entrypoint and the gate.

    :param apply_actor: passed as ``--as`` to plan and apply. Required whenever
        policy bundles are in the image.
    :param graph_removal_allowlist: graph ids the plan may delete.
    """
    return [
        kubernetes.core.v1.EnvVarArgs(name="OMNIGRAPH_APPLY_ACTOR", value=apply_actor),
        kubernetes.core.v1.EnvVarArgs(
            name="OMNIGRAPH_GRAPH_REMOVAL_ALLOWLIST",
            value=",".join(graph_removal_allowlist),
        ),
    ]


class OmnigraphLockHandoff(NamedTuple):
    """Handles to the lock-handoff resources, and the env the entrypoint reads."""

    lease: kubernetes.coordination.v1.Lease
    role_binding: kubernetes.rbac.v1.RoleBinding
    env: list[kubernetes.core.v1.EnvVarArgs]


def create_lock_handoff(  # noqa: PLR0913
    stack_info: StackInfo,
    namespace: str,
    k8s_global_labels: dict[str, str],
    service_account_name: str,
    pod_selector: dict[str, str],
    apply_actor: str,
    graph_removal_allowlist: list[str],
) -> OmnigraphLockHandoff:
    """Provision the unlock Lease and the server's access to it and to its pods.

    :param pod_selector: the Deployment's ``matchLabels``. The entrypoint
        refuses to boot unless its own pod matches the selector it is given,
        so this has to be the same dict the Deployment selects on.
    :param apply_actor: passed as ``--as`` to the entrypoint's plan and apply.
        Required whenever policy bundles are in the image.
    :param graph_removal_allowlist: graph ids the entrypoint may delete.
    """
    # PRE-CREATED, WITH NO HOLDER. RBAC cannot restrict `create` by resource
    # name, so letting the pod create its own Lease would mean granting create
    # on every Lease in the namespace. The holder takes this one with an
    # `update` carrying the resourceVersion it read. No `spec` is declared, so
    # Pulumi never owns the holder fields and a later `pulumi up` cannot reset
    # a live holder.
    lease = kubernetes.coordination.v1.Lease(
        f"omnigraph-unlock-lease-{stack_info.env_suffix}",
        metadata=kubernetes.meta.v1.ObjectMetaArgs(
            name=UNLOCK_LEASE_NAME,
            namespace=namespace,
            labels=k8s_global_labels,
        ),
    )
    role = kubernetes.rbac.v1.Role(
        f"omnigraph-server-lock-handoff-role-{stack_info.env_suffix}",
        metadata=kubernetes.meta.v1.ObjectMetaArgs(
            name=LOCK_HANDOFF_ROLE_NAME,
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
            # RBAC cannot scope `list` to a label selector, so this is every
            # pod in the namespace. Read-only, and nothing about scaling: the
            # server must not be able to change its own Deployment.
            kubernetes.rbac.v1.PolicyRuleArgs(
                api_groups=[""], resources=["pods"], verbs=["list"]
            ),
        ],
    )
    role_binding = kubernetes.rbac.v1.RoleBinding(
        f"omnigraph-server-lock-handoff-binding-{stack_info.env_suffix}",
        metadata=kubernetes.meta.v1.ObjectMetaArgs(
            name=LOCK_HANDOFF_ROLE_NAME,
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
                name=service_account_name,
                namespace=namespace,
            )
        ],
    )
    env = [
        kubernetes.core.v1.EnvVarArgs(
            name="OMNIGRAPH_UNLOCK_LEASE", value=UNLOCK_LEASE_NAME
        ),
        kubernetes.core.v1.EnvVarArgs(
            name="OMNIGRAPH_POD_SELECTOR", value=pod_selector_env(pod_selector)
        ),
        kubernetes.core.v1.EnvVarArgs(
            name="POD_NAME",
            value_from=kubernetes.core.v1.EnvVarSourceArgs(
                field_ref=kubernetes.core.v1.ObjectFieldSelectorArgs(
                    field_path="metadata.name"
                )
            ),
        ),
        *plan_env(apply_actor, graph_removal_allowlist),
    ]
    return OmnigraphLockHandoff(lease=lease, role_binding=role_binding, env=env)


def create_plan_gate(  # noqa: PLR0913
    stack_info: StackInfo,
    namespace: str,
    k8s_global_labels: dict[str, str],
    image: str | Output[str],
    aws_region: str,
    service_account_name: str,
    cluster_configmap_name: str,
    cluster_config_dir: str,
    actor_tokens_secret_name: str,
    actor_tokens_file: str,
    apply_actor: str,
    graph_removal_allowlist: list[str],
    config_hash: Output[str],
    depends_on: list[Resource],
) -> kubernetes.batch.v1.Job:
    """Provision the read-only pre-deploy plan gate.

    The entrypoint applies schema and policy changes on boot, after
    ``Recreate`` has stopped the old pod, so a change the apply refuses would
    be found by a pod that cannot start. This Job runs the image's own plan
    check (agent-kit ``docker/omnigraph_plan_gate.py``) against the served
    root first. The Deployment depends on it, so the same refusal fails the
    update while the old pod is still serving.

    `cluster plan` takes no writer lock, so this is not a second writer
    beside the running server, and it needs no Kubernetes access.

    It does not replace the entrypoint's check. The ``cluster.yaml`` ConfigMap
    is updated before this Job runs and a failed Job does not roll it back, so
    the next restart for any reason plans the same configuration again.

    :param actor_tokens_file: path of the actor-token map inside the mounted
        Secret. Group membership is rendered from it before planning, as on
        boot, so the plan is for the bundles the server would apply.
    :param config_hash: of ``cluster.yaml`` and the image. A Job's pod template
        is immutable, so a new hash replaces the Job, which is what makes it
        run again.
    """
    tokens_dir = actor_tokens_file.rpartition("/")[0]
    return kubernetes.batch.v1.Job(
        f"omnigraph-plan-gate-{stack_info.env_suffix}",
        # Unnamed, so the replacement is created before the old Job is removed.
        metadata=kubernetes.meta.v1.ObjectMetaArgs(
            namespace=namespace,
            labels=k8s_global_labels,
        ),
        spec=kubernetes.batch.v1.JobSpecArgs(
            # Planning is read-only, so a retry is free and rides out a
            # transient S3 or IRSA-credential failure.
            backoff_limit=2,
            ttl_seconds_after_finished=86400,
            template=kubernetes.core.v1.PodTemplateSpecArgs(
                metadata=kubernetes.meta.v1.ObjectMetaArgs(
                    labels={
                        **k8s_global_labels,
                        "app.kubernetes.io/name": "omnigraph-plan-gate",
                    },
                    annotations={"ol.mit.edu/config-hash": config_hash},
                ),
                spec=kubernetes.core.v1.PodSpecArgs(
                    restart_policy="Never",
                    # The server's IRSA identity, for read access to the
                    # root it plans against.
                    service_account_name=service_account_name,
                    containers=[
                        kubernetes.core.v1.ContainerArgs(
                            name="plan-gate",
                            image=image,
                            command=["/usr/local/bin/omnigraph_plan_gate.py"],
                            env=[
                                kubernetes.core.v1.EnvVarArgs(
                                    name="AWS_REGION", value=aws_region
                                ),
                                kubernetes.core.v1.EnvVarArgs(
                                    name="OMNIGRAPH_CLUSTER_DIR",
                                    value=cluster_config_dir,
                                ),
                                kubernetes.core.v1.EnvVarArgs(
                                    name="OMNIGRAPH_SERVER_BEARER_TOKENS_FILE",
                                    value=actor_tokens_file,
                                ),
                                *plan_env(apply_actor, graph_removal_allowlist),
                            ],
                            resources=kubernetes.core.v1.ResourceRequirementsArgs(
                                requests={"cpu": "100m", "memory": "256Mi"},
                                limits={"cpu": "1", "memory": "1Gi"},
                            ),
                            volume_mounts=[
                                kubernetes.core.v1.VolumeMountArgs(
                                    name="cluster-config",
                                    mount_path=f"{cluster_config_dir}/cluster.yaml",
                                    sub_path="cluster.yaml",
                                    read_only=True,
                                ),
                                kubernetes.core.v1.VolumeMountArgs(
                                    name="actor-tokens",
                                    mount_path=tokens_dir,
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
                        kubernetes.core.v1.VolumeArgs(
                            name="actor-tokens",
                            secret=kubernetes.core.v1.SecretVolumeSourceArgs(
                                secret_name=actor_tokens_secret_name,
                            ),
                        ),
                    ],
                ),
            ),
        ),
        opts=ResourceOptions(depends_on=depends_on),
    )
