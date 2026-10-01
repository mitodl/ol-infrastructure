"""Deploy the Gravitino posture probe as a CronJob and as a post-deploy Job.

The probe is ``scripts/check_gravitino_posture.py``; its docstring covers what
it asserts and why. This module is the deployment half, on the same pattern as
``applications/omnigraph/council_probe.py``: a stdlib script in a ConfigMap on a
stock Python image, with a non-zero exit as the signal.

Unlike council_probe, this pod needs a ServiceAccount, because check 3 reads the
rendered ``gravitino.conf`` out of the chart's ConfigMap. Its Role allows ``get``
on that one ConfigMap and nothing else. It mounts the namespace CA certificate,
never a client certificate, so it sees the management port the way any other pod
in the cluster does.

The post-deploy Job runs the same pod once per change to the server
configuration. Pulumi waits for a Job to complete, so a stack that would deploy
an unauthenticated catalog fails its ``pulumi up`` instead of waiting up to 15
minutes for the CronJob to notice.
"""

import hashlib
import json
from pathlib import Path

import pulumi_kubernetes as kubernetes
from pulumi import Output, Resource, ResourceOptions

from ol_infrastructure.lib.aws.eks_helper import cached_image_uri
from ol_infrastructure.lib.pulumi_helper import StackInfo

PROBE_IMAGE = cached_image_uri("python:3.12-slim")

POSTURE_NAME = "gravitino-posture"
SCRIPT_MOUNT_PATH = "/opt/gravitino-posture"
SCRIPT_FILENAME = "check_gravitino_posture.py"
CA_MOUNT_PATH = "/etc/gravitino-ca"
CA_FILENAME = "ca.crt"

# Every 15 minutes, per the deployment spec (P10). Keep it in the fast bucket of
# the ScheduledJobStale rules in infrastructure/grafana_alerting/metric_rules/
# eks_general.py.
POSTURE_SCHEDULE = "*/15 * * * *"
# Three requests at a 10s timeout each, plus pod start.
POSTURE_ACTIVE_DEADLINE_SECONDS = 120
POSTURE_BACKOFF_LIMIT = 1


def create_posture_probe(  # noqa: PLR0913
    stack_info: StackInfo,
    namespace: str,
    k8s_labels: dict[str, str],
    gravitino_host: str,
    iceberg_rest_port: int,
    management_port: int,
    tls_secret_name: str,
    config_map_name: str,
    config_map_key: str,
    expected_settings: Output[dict[str, str]],
    forbidden_settings: list[str],
    must_not_be_true_settings: list[str],
    gravitino_release: Resource,
    release_fingerprint: Output[str],
) -> kubernetes.batch.v1.CronJob:
    """Provision the posture probe CronJob and its post-deploy Job.

    :param expected_settings: Keys that must occur exactly once in the rendered
        ``gravitino.conf``, with these values.
    :param forbidden_settings: Keys that must not occur at all.
    :param must_not_be_true_settings: Keys that may occur but must not be ``true``.
    :param gravitino_release: The Helm release; the post-deploy Job runs after it.
    :param release_fingerprint: A digest of the release's values. A change re-runs
        the post-deploy Job.

    :returns: The CronJob.
    """
    script_body = (Path(__file__).parent / "scripts" / SCRIPT_FILENAME).read_text()

    script_config_map = kubernetes.core.v1.ConfigMap(
        f"gravitino-posture-script-{stack_info.env_suffix}",
        metadata=kubernetes.meta.v1.ObjectMetaArgs(
            namespace=namespace,
            labels=k8s_labels,
        ),
        data={SCRIPT_FILENAME: script_body},
        opts=ResourceOptions(
            replace_on_changes=["data"],
            delete_before_replace=False,
        ),
    )

    service_account = kubernetes.core.v1.ServiceAccount(
        f"gravitino-posture-service-account-{stack_info.env_suffix}",
        metadata=kubernetes.meta.v1.ObjectMetaArgs(
            name=POSTURE_NAME,
            namespace=namespace,
            labels=k8s_labels,
        ),
    )
    role = kubernetes.rbac.v1.Role(
        f"gravitino-posture-role-{stack_info.env_suffix}",
        metadata=kubernetes.meta.v1.ObjectMetaArgs(
            name=POSTURE_NAME,
            namespace=namespace,
            labels=k8s_labels,
        ),
        rules=[
            kubernetes.rbac.v1.PolicyRuleArgs(
                api_groups=[""],
                resources=["configmaps"],
                resource_names=[config_map_name],
                verbs=["get"],
            )
        ],
    )
    role_binding = kubernetes.rbac.v1.RoleBinding(
        f"gravitino-posture-role-binding-{stack_info.env_suffix}",
        metadata=kubernetes.meta.v1.ObjectMetaArgs(
            name=POSTURE_NAME,
            namespace=namespace,
            labels=k8s_labels,
        ),
        role_ref=kubernetes.rbac.v1.RoleRefArgs(
            api_group="rbac.authorization.k8s.io",
            kind="Role",
            name=role.metadata.name,
        ),
        subjects=[
            kubernetes.rbac.v1.SubjectArgs(
                kind="ServiceAccount",
                name=service_account.metadata.name,
                namespace=namespace,
            )
        ],
    )

    def pod_template(
        extra_annotations: Output[dict[str, str]] | None = None,
    ) -> kubernetes.core.v1.PodTemplateSpecArgs:
        return kubernetes.core.v1.PodTemplateSpecArgs(
            metadata=kubernetes.meta.v1.ObjectMetaArgs(
                labels={**k8s_labels, "app.kubernetes.io/name": POSTURE_NAME},
                annotations=extra_annotations,
            ),
            spec=kubernetes.core.v1.PodSpecArgs(
                restart_policy="Never",
                service_account_name=service_account.metadata.name,
                containers=[
                    kubernetes.core.v1.ContainerArgs(
                        name="check-gravitino-posture",
                        image=PROBE_IMAGE,
                        command=["python", f"{SCRIPT_MOUNT_PATH}/{SCRIPT_FILENAME}"],
                        env=[
                            kubernetes.core.v1.EnvVarArgs(
                                name="GRAVITINO_HOST", value=gravitino_host
                            ),
                            kubernetes.core.v1.EnvVarArgs(
                                name="ICEBERG_REST_PORT", value=str(iceberg_rest_port)
                            ),
                            kubernetes.core.v1.EnvVarArgs(
                                name="MANAGEMENT_PORT", value=str(management_port)
                            ),
                            kubernetes.core.v1.EnvVarArgs(
                                name="GRAVITINO_CA_FILE",
                                value=f"{CA_MOUNT_PATH}/{CA_FILENAME}",
                            ),
                            kubernetes.core.v1.EnvVarArgs(
                                name="GRAVITINO_NAMESPACE", value=namespace
                            ),
                            kubernetes.core.v1.EnvVarArgs(
                                name="GRAVITINO_CONFIG_MAP", value=config_map_name
                            ),
                            kubernetes.core.v1.EnvVarArgs(
                                name="GRAVITINO_CONFIG_KEY", value=config_map_key
                            ),
                            kubernetes.core.v1.EnvVarArgs(
                                name="EXPECTED_SETTINGS",
                                value=expected_settings.apply(json.dumps),
                            ),
                            kubernetes.core.v1.EnvVarArgs(
                                name="FORBIDDEN_SETTINGS",
                                value=json.dumps(forbidden_settings),
                            ),
                            kubernetes.core.v1.EnvVarArgs(
                                name="MUST_NOT_BE_TRUE_SETTINGS",
                                value=json.dumps(must_not_be_true_settings),
                            ),
                        ],
                        volume_mounts=[
                            kubernetes.core.v1.VolumeMountArgs(
                                name="script",
                                mount_path=SCRIPT_MOUNT_PATH,
                                read_only=True,
                            ),
                            kubernetes.core.v1.VolumeMountArgs(
                                name="ca",
                                mount_path=CA_MOUNT_PATH,
                                read_only=True,
                            ),
                        ],
                        security_context=kubernetes.core.v1.SecurityContextArgs(
                            run_as_non_root=True,
                            run_as_user=1000,
                            run_as_group=1000,
                            allow_privilege_escalation=False,
                        ),
                        resources=kubernetes.core.v1.ResourceRequirementsArgs(
                            requests={"cpu": "25m", "memory": "32Mi"},
                            limits={"cpu": "250m", "memory": "128Mi"},
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
                    # Only the CA certificate is projected. The server Secret also
                    # holds the server's private key and keystores, which this pod
                    # has no use for.
                    kubernetes.core.v1.VolumeArgs(
                        name="ca",
                        secret=kubernetes.core.v1.SecretVolumeSourceArgs(
                            secret_name=tls_secret_name,
                            items=[
                                kubernetes.core.v1.KeyToPathArgs(
                                    key=CA_FILENAME, path=CA_FILENAME
                                )
                            ],
                        ),
                    ),
                ],
            ),
        )

    job_dependencies = [script_config_map, role_binding]

    cron_job = kubernetes.batch.v1.CronJob(
        f"gravitino-posture-{stack_info.env_suffix}",
        metadata=kubernetes.meta.v1.ObjectMetaArgs(
            name=POSTURE_NAME,
            namespace=namespace,
            labels=k8s_labels,
        ),
        spec=kubernetes.batch.v1.CronJobSpecArgs(
            schedule=POSTURE_SCHEDULE,
            concurrency_policy="Forbid",
            starting_deadline_seconds=300,
            successful_jobs_history_limit=1,
            failed_jobs_history_limit=5,
            job_template=kubernetes.batch.v1.JobTemplateSpecArgs(
                metadata=kubernetes.meta.v1.ObjectMetaArgs(labels=k8s_labels),
                spec=kubernetes.batch.v1.JobSpecArgs(
                    backoff_limit=POSTURE_BACKOFF_LIMIT,
                    active_deadline_seconds=POSTURE_ACTIVE_DEADLINE_SECONDS,
                    template=pod_template(),
                ),
            ),
        ),
        opts=ResourceOptions(depends_on=job_dependencies),
    )

    # A Job's pod template is immutable, so a changed annotation makes Pulumi
    # replace the Job and the probe runs again. The fingerprint covers the whole
    # Helm values document as well as the assertions, because a values change
    # outside the security keys (e.g. a duplicate pushed through
    # additionalConfigItems) is exactly what can silently undo one of them.
    posture_fingerprint = Output.all(release_fingerprint, expected_settings).apply(
        lambda args: hashlib.sha256(
            json.dumps(
                [
                    args[0],
                    args[1],
                    forbidden_settings,
                    must_not_be_true_settings,
                    script_body,
                ],
                sort_keys=True,
            ).encode()
        ).hexdigest()[:16]
    )
    kubernetes.batch.v1.Job(
        f"gravitino-posture-post-deploy-{stack_info.env_suffix}",
        metadata=kubernetes.meta.v1.ObjectMetaArgs(
            namespace=namespace,
            labels=k8s_labels,
        ),
        spec=kubernetes.batch.v1.JobSpecArgs(
            backoff_limit=POSTURE_BACKOFF_LIMIT,
            active_deadline_seconds=POSTURE_ACTIVE_DEADLINE_SECONDS,
            template=pod_template(
                posture_fingerprint.apply(
                    lambda fingerprint: {"ol.mit.edu/posture-fingerprint": fingerprint}
                )
            ),
        ),
        opts=ResourceOptions(depends_on=[*job_dependencies, gravitino_release]),
    )

    return cron_job
