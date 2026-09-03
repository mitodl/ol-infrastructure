"""Deploy the council-health synthetic-probe CronJob.

The probe itself is ``scripts/check_council_health.py``; its module docstring
covers what it does and why. This module is the deployment half: schedule,
image, and how the pod gets the one credential it needs.

WHY NO IN-POD VAULT LOGIN, UNLIKE token_sync.py

token_sync authenticates to Vault itself because it *writes* a Vault path at
runtime. This job only *reads* one bearer token, and that credential already
has a standard delivery path: the same Vault-secret-sync arrangement every
other non-human witan token (`ci-token`, `admin-token`, `service-token`) uses
— ``__main__.py`` writes the token to Vault from SOPS, an ``OLVaultK8SSecret``
syncs it into a plain Kubernetes Secret, and this CronJob mounts it with an
ordinary ``secretKeyRef``. No ServiceAccount, no Vault Kubernetes auth role,
no ``automountServiceAccountToken`` — this pod carries no credential beyond
the one env var.

WHY A STOCK PYTHON IMAGE AND A CONFIGMAP

Same reasoning as token_sync.py: the script is stdlib-only, so there is
nothing to install and no image to build, publish, scan, or keep patched.
Shipping it as a ConfigMap against ``python:3.12-slim`` means a change to the
probe is a change to this stack and nothing else.

OPTIONAL, THE SAME WAY THE ADMIN PRINCIPAL IS

`svc-witan-probe` is opt-in per environment (see `__main__.py`): an
environment whose operator has not yet minted a probe token gets no CronJob at
all here, rather than one that crash-loops on a missing Secret.
"""

from pathlib import Path

import pulumi_kubernetes as kubernetes
from pulumi import Output, Resource, ResourceOptions

from ol_infrastructure.lib.aws.eks_helper import cached_image_uri
from ol_infrastructure.lib.pulumi_helper import StackInfo

PROBE_IMAGE = cached_image_uri("python:3.12-slim")

CRONJOB_NAME = "witan-council-probe"
SCRIPT_MOUNT_PATH = "/opt/witan-council-probe"
SCRIPT_FILENAME = "check_council_health.py"

# Every 15 minutes. The window this closes is bounded by cadence, not by the
# request itself (one query, ~15s ceiling below) — 15m keeps the worst-case
# "council is down and nobody has restarted a pod or made a call" detection
# window well inside the existing staleness rule's 6h fast-bucket threshold
# (eks_general.py), so a genuinely stuck CronJob controller is caught by that
# rule long before a single missed run would be.
DEFAULT_PROBE_SCHEDULE = "*/15 * * * *"

# One HTTP call with its own 15s client-side timeout (script module docstring).
# A run that has not finished in a minute is wedged on something the client
# timeout should already have caught.
PROBE_ACTIVE_DEADLINE_SECONDS = 60

# The script is a single idempotent read; a retry is always safe, and two of
# them ride out one dropped connection without waiting for the next tick.
PROBE_BACKOFF_LIMIT = 1


def create_council_probe(  # noqa: PLR0913
    stack_info: StackInfo,
    namespace: str,
    k8s_global_labels: dict[str, str],
    omnigraph_server_addr: str | Output[str],
    graph_id: str,
    schedule: str,
    probe_token_secret_name: str,
    probe_token_secret_key: str,
    probe_token_secret: Resource,
) -> kubernetes.batch.v1.CronJob:
    """Provision the CronJob that runs the council-health probe on a schedule.

    Callers gate this on the probe token actually being provisioned (see
    ``__main__.py``) — there is no internal check here, because an
    unconditional call with no token would either crash-loop or (worse) run
    unauthenticated and prove nothing about the identity path being tested.
    """
    script_body = (Path(__file__).parent / "scripts" / SCRIPT_FILENAME).read_text()

    # Same replace-on-change shape as token_sync.py's script ConfigMap: a
    # mutated ConfigMap propagates to already-scheduled pods on the kubelet's
    # own schedule, so a script edit has to roll a NEW ConfigMap rather than
    # risk a run picking up half-updated content mid-propagation.
    script_config_map = kubernetes.core.v1.ConfigMap(
        f"witan-council-probe-script-{stack_info.env_suffix}",
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
            # No ServiceAccount token needed — see the module docstring. The
            # default SA is left mounted at its usual read-only Kubernetes-API
            # scope, which this pod never calls; explicitly disabling it would
            # save nothing this pod can leak, since it makes zero K8s API calls.
            containers=[
                kubernetes.core.v1.ContainerArgs(
                    name="check-council-health",
                    image=PROBE_IMAGE,
                    command=["python", f"{SCRIPT_MOUNT_PATH}/{SCRIPT_FILENAME}"],
                    env=[
                        kubernetes.core.v1.EnvVarArgs(
                            name="OMNIGRAPH_SERVER_ADDR", value=omnigraph_server_addr
                        ),
                        kubernetes.core.v1.EnvVarArgs(
                            name="OMNIGRAPH_GRAPH_ID", value=graph_id
                        ),
                        kubernetes.core.v1.EnvVarArgs(
                            name="OMNIGRAPH_BEARER_TOKEN",
                            value_from=kubernetes.core.v1.EnvVarSourceArgs(
                                secret_key_ref=kubernetes.core.v1.SecretKeySelectorArgs(
                                    name=probe_token_secret_name,
                                    key=probe_token_secret_key,
                                )
                            ),
                        ),
                    ],
                    volume_mounts=[
                        kubernetes.core.v1.VolumeMountArgs(
                            name="script",
                            mount_path=SCRIPT_MOUNT_PATH,
                            read_only=True,
                        )
                    ],
                    security_context=kubernetes.core.v1.SecurityContextArgs(
                        run_as_non_root=True,
                        run_as_user=1000,
                        run_as_group=1000,
                    ),
                    # One HTTP call. The limits are here to make it
                    # evictable-last rather than because it is anywhere near
                    # them.
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
                )
            ],
        ),
    )

    return kubernetes.batch.v1.CronJob(
        f"witan-council-probe-{stack_info.env_suffix}",
        metadata=kubernetes.meta.v1.ObjectMetaArgs(
            name=CRONJOB_NAME,
            namespace=namespace,
            labels=k8s_global_labels,
        ),
        spec=kubernetes.batch.v1.CronJobSpecArgs(
            schedule=schedule,
            # A run overlapping the previous one would just mean two identical
            # reads in flight; Forbid keeps the job history and the failure
            # signal legible rather than piling up concurrent runs if the
            # server is genuinely slow to answer.
            concurrency_policy="Forbid",
            starting_deadline_seconds=300,
            successful_jobs_history_limit=1,
            # A failed run is the one whose logs somebody needs, and the
            # failure IS the alert signal this whole probe exists to produce
            # (see the module docstring) — keep more than the successes.
            failed_jobs_history_limit=5,
            job_template=kubernetes.batch.v1.JobTemplateSpecArgs(
                metadata=kubernetes.meta.v1.ObjectMetaArgs(labels=k8s_global_labels),
                spec=kubernetes.batch.v1.JobSpecArgs(
                    backoff_limit=PROBE_BACKOFF_LIMIT,
                    active_deadline_seconds=PROBE_ACTIVE_DEADLINE_SECONDS,
                    template=pod_template,
                ),
            ),
        ),
        opts=ResourceOptions(depends_on=[script_config_map, probe_token_secret]),
    )
