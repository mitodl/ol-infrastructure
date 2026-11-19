"""Deploy the CronJob that counts each graph's version objects every hour.

The count itself is ``scripts/count_store_versions.py``; its docstring covers
what is counted and why. The alert rules that read its lines are in
``infrastructure/grafana_alerting/log_rules/omnigraph_versions.py``.

WHY A LOG LINE AND NOT A METRIC

The stack has no metrics push path for a CronJob, and Loki already collects
every pod's stdout in QA and Production. The rules unwrap the counts from the
lines, the same way ``WitanCodeBridgeNoBindings`` unwraps the CI indexer's
``bindings=`` counts. CI ships no logs, so there the lines are only readable
with ``kubectl logs``.

WHY THE AWS CLI IMAGE AND THE SERVER'S SERVICEACCOUNT

The count needs an S3 listing, which the standard library cannot sign. The
``amazon/aws-cli`` image is already this stack's image for S3 steps
(storage_migration.py) and also ships a system ``python3``, so the script runs
there unchanged from a ConfigMap, with nothing to build. It runs as the
``omnigraph-server`` IRSA ServiceAccount, as the migration's S3 steps do. That
identity can also write the bucket, and this job only lists it; a read-only
role would be a separate IAM role and binding for one ``ListBucket`` call.
The same ServiceAccount is bound to the unlock Lease and pod listing
(lock_handoff.py), so the pod does not mount its Kubernetes token: the IRSA
token is a separate projected volume, and this job makes no Kubernetes call.

The listing is read-only and takes no omnigraph lock, so it runs whether the
server is up or stopped for the nightly window.
"""

from pathlib import Path

import pulumi_kubernetes as kubernetes
from pulumi import Output, ResourceOptions

from ol_infrastructure.lib.pulumi_helper import StackInfo

CRONJOB_NAME = "omnigraph-version-census"
SCRIPT_MOUNT_PATH = "/opt/omnigraph-version-census"
SCRIPT_FILENAME = "count_store_versions.py"

# Hourly, so the 24h growth rule compares samples a day apart and one missed
# run leaves the budget rule a sample inside its 2h window. Production's
# listing is about 140 S3 LIST pages (136,807 objects on 2026-10-28) and QA's
# finished in about 10s, so the cost is negligible.
DEFAULT_CENSUS_SCHEDULE = "41 * * * *"

# A listing that has not finished in 10 minutes is a wedged connection, not a
# large store.
CENSUS_ACTIVE_DEADLINE_SECONDS = 600

CENSUS_JOB_TTL_SECONDS = 3600


def create_version_census(  # noqa: PLR0913
    stack_info: StackInfo,
    namespace: str,
    k8s_global_labels: dict[str, str],
    aws_cli_image: str,
    service_account_name: str,
    storage_root: Output[str],
    schedule: str,
) -> kubernetes.batch.v1.CronJob:
    """Provision the hourly version-count CronJob.

    :param service_account_name: an IRSA ServiceAccount that may list the
        graph bucket.
    :param storage_root: the root the cluster serves
        (``s3://<bucket>/<storage_prefix>``); the script lists its
        ``graphs/`` prefix.
    """
    script_body = (Path(__file__).parent / "scripts" / SCRIPT_FILENAME).read_text()
    # Replaced rather than mutated, as council_probe.py's ConfigMap is, so a
    # run never reads a half-propagated script.
    script_config_map = kubernetes.core.v1.ConfigMap(
        f"omnigraph-version-census-script-{stack_info.env_suffix}",
        metadata=kubernetes.meta.v1.ObjectMetaArgs(
            namespace=namespace,
            labels=k8s_global_labels,
        ),
        data={SCRIPT_FILENAME: script_body},
        opts=ResourceOptions(
            replace_on_changes=["data"],
            delete_before_replace=False,
        ),
    )

    pod_template = kubernetes.core.v1.PodTemplateSpecArgs(
        metadata=kubernetes.meta.v1.ObjectMetaArgs(
            labels={
                **k8s_global_labels,
                "app.kubernetes.io/name": CRONJOB_NAME,
            },
        ),
        spec=kubernetes.core.v1.PodSpecArgs(
            restart_policy="Never",
            service_account_name=service_account_name,
            automount_service_account_token=False,
            security_context=kubernetes.core.v1.PodSecurityContextArgs(
                run_as_non_root=True,
                run_as_user=1000,
                run_as_group=1000,
            ),
            containers=[
                kubernetes.core.v1.ContainerArgs(
                    name="count-versions",
                    image=aws_cli_image,
                    command=["python3", f"{SCRIPT_MOUNT_PATH}/{SCRIPT_FILENAME}"],
                    env=[
                        kubernetes.core.v1.EnvVarArgs(
                            name="OMNIGRAPH_STORAGE_ROOT", value=storage_root
                        ),
                        kubernetes.core.v1.EnvVarArgs(
                            name="AWS_REGION", value="us-east-1"
                        ),
                        # uid 1000 has no home in this image, and the CLI
                        # writes a cache under $HOME.
                        kubernetes.core.v1.EnvVarArgs(
                            name="HOME",
                            value="/tmp",  # noqa: S108
                        ),
                    ],
                    volume_mounts=[
                        kubernetes.core.v1.VolumeMountArgs(
                            name="script",
                            mount_path=SCRIPT_MOUNT_PATH,
                            read_only=True,
                        ),
                        kubernetes.core.v1.VolumeMountArgs(
                            name="work",
                            mount_path="/tmp",  # noqa: S108
                        ),
                    ],
                    resources=kubernetes.core.v1.ResourceRequirementsArgs(
                        requests={"cpu": "50m", "memory": "128Mi"},
                        limits={"cpu": "500m", "memory": "512Mi"},
                    ),
                )
            ],
            volumes=[
                kubernetes.core.v1.VolumeArgs(
                    name="script",
                    config_map=kubernetes.core.v1.ConfigMapVolumeSourceArgs(
                        name=script_config_map.metadata.name,
                        default_mode=0o555,
                    ),
                ),
                kubernetes.core.v1.VolumeArgs(
                    name="work",
                    empty_dir=kubernetes.core.v1.EmptyDirVolumeSourceArgs(),
                ),
            ],
        ),
    )

    return kubernetes.batch.v1.CronJob(
        f"omnigraph-version-census-{stack_info.env_suffix}",
        metadata=kubernetes.meta.v1.ObjectMetaArgs(
            name=CRONJOB_NAME,
            namespace=namespace,
            labels=k8s_global_labels,
        ),
        spec=kubernetes.batch.v1.CronJobSpecArgs(
            schedule=schedule,
            concurrency_policy="Forbid",
            starting_deadline_seconds=300,
            successful_jobs_history_limit=1,
            failed_jobs_history_limit=3,
            job_template=kubernetes.batch.v1.JobTemplateSpecArgs(
                metadata=kubernetes.meta.v1.ObjectMetaArgs(labels=k8s_global_labels),
                spec=kubernetes.batch.v1.JobSpecArgs(
                    # A read-only listing, so a retry is always safe, and one
                    # rides out a single S3 error without a failed Job.
                    backoff_limit=1,
                    active_deadline_seconds=CENSUS_ACTIVE_DEADLINE_SECONDS,
                    # WorkloadJobFailed* fires for as long as a failed Job
                    # exists. Deleting finished Jobs after an hour lets a
                    # one-off failure clear itself. One that fails every run
                    # mostly keeps it firing, with gaps when runs fail at
                    # different points; ScheduledJobStaleFast* covers it
                    # after 6h either way. The lines are in Loki by then.
                    ttl_seconds_after_finished=CENSUS_JOB_TTL_SECONDS,
                    template=pod_template,
                ),
            ),
        ),
        opts=ResourceOptions(depends_on=[script_config_map]),
    )
