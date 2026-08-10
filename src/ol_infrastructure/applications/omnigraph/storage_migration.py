"""The storage-format migration Job: rebuild every graph at a new root.

omnigraph storage is strict-single-version, so an image whose binary bumps the
on-disk format cannot be rolled out — every graph must be exported by the old
binary and reloaded by the new one at a *different* root, with the old root
left intact as the rollback. ``docs/omnigraph-storage-format-upgrade-runbook.md``
is the procedure; this provisions the part of it worth having a machine do.

OPT-IN, AND ABSENT BY DEFAULT. Nothing here exists unless
``omnigraph:migrate_from_image`` names the image to migrate *from*. A migration
is a scheduled outage, not something a routine ``pulumi up`` should be able to
start, and a Job that only exists while an operator is deliberately holding
that config open cannot fire by accident. Clearing the config removes it.

ONE POD, BOTH BINARIES. An initContainer on the OLD image copies its
``omnigraph`` binary into a shared ``emptyDir``; the main container runs the
NEW image and sees both. That is the whole reason this is worth automating: the
runbook's manual form uses two ``kubectl run`` pods and moves every export
through the operator's workstation, which is the slowest step and the only one
that fails silently — a truncated ``kubectl exec > file`` yields a short export
and ``load`` reports success over it. Here the bytes go S3 → pod → S3.

The initContainer needs nothing but ``cp``, so it works against any historical
image, including ones predating the ``python3-minimal`` the main container's
script needs.

WHAT IT WILL NOT DO. It does not repoint the live cluster. That is
``omnigraph:storage_prefix`` — Pulumi config — and this pod holds no Pulumi
credentials; writing the ConfigMap instead would make it a second writer of a
path Pulumi owns, the exact failure ``sync_actor_tokens.py`` documents on the
token map. The Job verifies per-table row counts and stops, leaving a verdict
in its logs and at ``/tmp/migration-verdict.json``. Cutting over stays a
deliberate act by whoever is running the migration.
"""

from pathlib import Path
from typing import NamedTuple

import pulumi_kubernetes as kubernetes
from pulumi import Output, ResourceOptions

from ol_infrastructure.applications.omnigraph.maintenance import OmnigraphMaintenance
from ol_infrastructure.lib.pulumi_helper import StackInfo

SCRIPT_FILENAME = "migrate_storage_format.py"
SCRIPT_MOUNT_PATH = "/etc/omnigraph/migration"
SHARED_BIN_PATH = "/shared-bin"
OLD_BINARY_PATH = f"{SHARED_BIN_PATH}/omnigraph-old"

# Where the server image bakes its schemas. The rebuilt cluster config is
# staged from these, so it declares the same schemas the running cluster does.
SCHEMA_DIR = "/etc/omnigraph/cluster"

# The break-glass maintenance principal (ADR-0005 path b). `cluster import` and
# `cluster apply` are actor-bound, and this is the account the Cedar bundles
# grant those to.
MIGRATION_ACTOR = "svc-witan-admin"

# A rebuild reads and rewrites every graph in the cluster. Six hours is far
# beyond any measured run at current store sizes and exists to stop a wedged S3
# connection holding an outage open indefinitely, not to bound real work.
ACTIVE_DEADLINE_SECONDS = 21600

# NO RETRY. A failed migration is a clean stop that an operator must look at —
# the old root is untouched and the cluster still serves it. Re-running
# automatically would start a second rebuild over a half-finished new root
# while nobody is reading the logs.
BACKOFF_LIMIT = 0

# Keep the finished pod. Its logs are the evidence the cutover decision rests
# on, and a Job that tidied itself away on success would take them with it.
TTL_SECONDS_AFTER_FINISHED = 604800

# Disk for `/tmp`, which holds EVERY graph's JSONL export simultaneously —
# requested so the scheduler places this pod somewhere that can hold them, and
# capped so an overrun evicts this pod rather than the node.
#
# 20Gi is a deliberate over-provision against a cluster whose graphs the
# upgrade runbook records as effectively unpopulated as of 2026-08-05. It is
# not a measurement, and it is the number to revisit first if the Job is ever
# evicted for disk: re-measure from a real export
# (`wc -c /tmp/export/*.jsonl` in the pod) rather than doubling it blindly.
EXPORT_STORAGE_SIZE = "20Gi"


def script_source() -> str:
    """Read the migration script out of the tree at Pulumi time.

    Same shape as the token-sync script's packaging: the file lives beside this
    module so it is reviewable, testable and diffable as Python rather than as
    a string embedded in a resource definition.
    """
    return (Path(__file__).parent / "scripts" / SCRIPT_FILENAME).read_text()


class OmnigraphStorageMigration(NamedTuple):
    """Handles to the provisioned migration resources."""

    script_config_map: kubernetes.core.v1.ConfigMap
    job: kubernetes.batch.v1.Job


def create_storage_migration(  # noqa: PLR0913
    stack_info: StackInfo,
    namespace: str,
    k8s_global_labels: dict[str, str],
    old_image: str,
    new_image: str | Output[str],
    old_storage_root: str | Output[str],
    new_storage_root: str | Output[str],
    new_storage_prefix: str,
    cluster_configmap_name: str,
    service_account_name: str,
    maintenance: OmnigraphMaintenance | None = None,
    opts: ResourceOptions | None = None,
) -> OmnigraphStorageMigration:
    """Provision the one-shot rebuild Job.

    ``old_storage_root`` is the root the cluster serves **now** — which is the
    bucket root only until the first cutover. After one, it is
    ``s3://<bucket>/fmt<N>``, and the next migration has to export from there.
    ``new_storage_root`` is where this rebuild writes.

    THE TWO ARE SEPARATE INPUTS ON PURPOSE. Deriving the destination from the
    source (``f"{old}/{prefix}"``) is correct exactly once: it produces
    ``s3://bucket/fmt6`` from a bare bucket, and ``s3://bucket/fmt5/fmt6`` from
    an already-migrated one — a nested root nobody serves, exported from the
    wrong place. Both roots are computed by the caller from the *bucket*, which
    is also what keeps them inside the IRSA grant (``<bucket-arn>/*``), the
    backups and the object versioning.

    ``new_storage_prefix`` is the same ``fmt<N>`` that will later become
    ``omnigraph:storage_prefix`` at cutover; it names the Job so
    ``kubectl get jobs`` says which rebuild this was.

    ``maintenance`` is the pair of sweeps to order this Job behind. They write
    directly to the store, so a tick between the export and the verification
    mutates a root the migration has declared frozen — and Pulumi would
    otherwise be free to create the Job in parallel with the updates that
    suspend them.
    """
    script_config_map = kubernetes.core.v1.ConfigMap(
        f"omnigraph-storage-migration-script-{stack_info.env_suffix}",
        metadata=kubernetes.meta.v1.ObjectMetaArgs(
            name="omnigraph-storage-migration-script",
            namespace=namespace,
            labels=k8s_global_labels,
        ),
        data={SCRIPT_FILENAME: script_source()},
        opts=opts,
    )

    # Ordered behind the suspension of both sweeps. `depends_on` is the only
    # thing that sequences these — the Job references neither CronJob, so
    # Pulumi is otherwise free to create it while `suspend=true` is still being
    # applied, leaving a window in which a tick can fire against the old root.
    job_opts = ResourceOptions.merge(
        opts,
        ResourceOptions(
            depends_on=[maintenance.optimize, maintenance.cleanup]
            if maintenance is not None
            else []
        ),
    )

    job = kubernetes.batch.v1.Job(
        f"omnigraph-storage-migration-{stack_info.env_suffix}",
        metadata=kubernetes.meta.v1.ObjectMetaArgs(
            # Named for the target format so a second migration cannot collide
            # with a completed one still being kept for its logs, and so
            # `kubectl get jobs` says which rebuild this was.
            name=f"omnigraph-migrate-{new_storage_prefix}",
            namespace=namespace,
            labels=k8s_global_labels,
        ),
        spec=kubernetes.batch.v1.JobSpecArgs(
            backoff_limit=BACKOFF_LIMIT,
            active_deadline_seconds=ACTIVE_DEADLINE_SECONDS,
            ttl_seconds_after_finished=TTL_SECONDS_AFTER_FINISHED,
            template=kubernetes.core.v1.PodTemplateSpecArgs(
                metadata=kubernetes.meta.v1.ObjectMetaArgs(labels=k8s_global_labels),
                spec=kubernetes.core.v1.PodSpecArgs(
                    restart_policy="Never",
                    # The same identity the server runs as, which is what
                    # carries the IRSA grant on the bucket. The migration
                    # touches exactly the storage the server already can.
                    service_account_name=service_account_name,
                    security_context=kubernetes.core.v1.PodSecurityContextArgs(
                        run_as_non_root=True,
                        run_as_user=1000,
                        run_as_group=1000,
                        fs_group=1000,
                    ),
                    init_containers=[
                        kubernetes.core.v1.ContainerArgs(
                            name="stage-old-binary",
                            image=old_image,
                            # `cp` only — deliberately the whole contract with
                            # the old image, so this works against any release
                            # regardless of what else that image carries.
                            command=[
                                "cp",
                                "/usr/local/bin/omnigraph",
                                OLD_BINARY_PATH,
                            ],
                            volume_mounts=[
                                kubernetes.core.v1.VolumeMountArgs(
                                    name="shared-bin", mount_path=SHARED_BIN_PATH
                                )
                            ],
                        )
                    ],
                    containers=[
                        kubernetes.core.v1.ContainerArgs(
                            name="migrate",
                            image=new_image,
                            command=[
                                "python3",
                                f"{SCRIPT_MOUNT_PATH}/{SCRIPT_FILENAME}",
                            ],
                            env=[
                                kubernetes.core.v1.EnvVarArgs(
                                    name="OMNIGRAPH_OLD_BINARY",
                                    value=OLD_BINARY_PATH,
                                ),
                                kubernetes.core.v1.EnvVarArgs(
                                    name="OMNIGRAPH_NEW_BINARY",
                                    value="/usr/local/bin/omnigraph",
                                ),
                                kubernetes.core.v1.EnvVarArgs(
                                    name="OMNIGRAPH_OLD_ROOT",
                                    value=old_storage_root,
                                ),
                                kubernetes.core.v1.EnvVarArgs(
                                    name="OMNIGRAPH_NEW_ROOT",
                                    value=new_storage_root,
                                ),
                                kubernetes.core.v1.EnvVarArgs(
                                    name="OMNIGRAPH_MIGRATION_ACTOR",
                                    value=MIGRATION_ACTOR,
                                ),
                                kubernetes.core.v1.EnvVarArgs(
                                    name="OMNIGRAPH_CLUSTER_CONFIG",
                                    # The LIVE cluster config, mounted rather
                                    # than re-derived: the `code-<repo>` ids
                                    # are derived values, and rebuilding that
                                    # list by hand is how a repo gets left
                                    # behind at the old root.
                                    value=f"{SCRIPT_MOUNT_PATH}/live/cluster.yaml",
                                ),
                                kubernetes.core.v1.EnvVarArgs(
                                    name="OMNIGRAPH_SCHEMA_DIR", value=SCHEMA_DIR
                                ),
                                kubernetes.core.v1.EnvVarArgs(
                                    name="AWS_REGION", value="us-east-1"
                                ),
                            ],
                            volume_mounts=[
                                kubernetes.core.v1.VolumeMountArgs(
                                    name="shared-bin",
                                    mount_path=SHARED_BIN_PATH,
                                    read_only=True,
                                ),
                                kubernetes.core.v1.VolumeMountArgs(
                                    name="script",
                                    mount_path=SCRIPT_MOUNT_PATH,
                                    read_only=True,
                                ),
                                kubernetes.core.v1.VolumeMountArgs(
                                    name="live-cluster-config",
                                    mount_path=f"{SCRIPT_MOUNT_PATH}/live",
                                    read_only=True,
                                ),
                                kubernetes.core.v1.VolumeMountArgs(
                                    name="work",
                                    mount_path="/tmp",  # noqa: S108
                                ),
                            ],
                            # Memory is sized for a single graph's load, not
                            # the cluster's — the exports go to disk rather
                            # than being held twice.
                            #
                            # EPHEMERAL-STORAGE IS REQUESTED, NOT JUST CAPPED.
                            # `/tmp` holds EVERY graph's export at once, and an
                            # emptyDir draws on the node's filesystem: without
                            # a request the scheduler can place this on a node
                            # that cannot hold the exports, and filling a node
                            # evicts whatever else is running there. The
                            # request makes it a scheduling constraint; the
                            # limit and the volume's own `size_limit` make an
                            # overrun evict this pod alone.
                            resources=kubernetes.core.v1.ResourceRequirementsArgs(
                                requests={
                                    "cpu": "250m",
                                    "memory": "512Mi",
                                    "ephemeral-storage": EXPORT_STORAGE_SIZE,
                                },
                                limits={
                                    "cpu": "2",
                                    "memory": "4Gi",
                                    "ephemeral-storage": EXPORT_STORAGE_SIZE,
                                },
                            ),
                        )
                    ],
                    volumes=[
                        kubernetes.core.v1.VolumeArgs(
                            name="shared-bin",
                            empty_dir=kubernetes.core.v1.EmptyDirVolumeSourceArgs(),
                        ),
                        kubernetes.core.v1.VolumeArgs(
                            name="work",
                            empty_dir=kubernetes.core.v1.EmptyDirVolumeSourceArgs(
                                size_limit=EXPORT_STORAGE_SIZE,
                            ),
                        ),
                        kubernetes.core.v1.VolumeArgs(
                            name="script",
                            config_map=kubernetes.core.v1.ConfigMapVolumeSourceArgs(
                                name=script_config_map.metadata.name,
                                default_mode=0o555,
                            ),
                        ),
                        kubernetes.core.v1.VolumeArgs(
                            name="live-cluster-config",
                            config_map=kubernetes.core.v1.ConfigMapVolumeSourceArgs(
                                name=cluster_configmap_name,
                            ),
                        ),
                    ],
                ),
            ),
        ),
        opts=job_opts,
    )

    return OmnigraphStorageMigration(script_config_map=script_config_map, job=job)
