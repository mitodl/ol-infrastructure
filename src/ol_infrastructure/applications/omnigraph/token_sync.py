"""Deploy the Keycloak realm -> actor-token sync job.

The job itself is ``scripts/sync_actor_tokens.py``; its module docstring covers
what it computes and why. This module is the deployment half: the identity it
runs as, the Vault role that lets it write, and the schedule it runs on.

WHY AN IN-CLUSTER CRONJOB RATHER THAN CONCOURSE

The job's whole purpose is one privileged write to
``secret-operations/witan/actor-tokens``. In-cluster it earns that privilege by
being the pod it is: Vault's Kubernetes auth exchanges the pod's ServiceAccount
token for a Vault token carrying exactly ``token_sync_policy.hcl``, and the
credential is minted per run and expires on its own. From Concourse the same
write needs a long-lived Vault credential provisioned into CI, which puts a
build system on the write path of the auth secret for the whole graph — a
strictly larger blast radius for no gain. The pod also needs nothing from a
Concourse worker: it clones nothing and builds nothing, it makes two HTTPS
calls. Everything else this write interacts with — the Vault path, the VSO sync
that renders it, the Deployment that restarts on it — is already owned by this
stack, so the writer belongs next to them.

WHY A STOCK PYTHON IMAGE AND A CONFIGMAP

The script is stdlib-only by design, so there is nothing to install and no
image to build, publish, scan, or keep patched. Shipping it as a ConfigMap
against ``python:3.12-slim`` (through the account's Docker Hub pull-through
cache, like vector_log_proxy) means a change to the script is a change to this
stack and nothing else — no ECR repository, no Concourse build job, no
image-tag plumbing between two repos. The sibling witan CI indexer
(``applications/witan/ci_indexer.py``) does use a built image, but it has to:
it runs agent-kit's own ``witan-ci-index`` entrypoint and must stay in lockstep
with the tier's release. This job shares no code with agent-kit beyond one
15-line function it deliberately restates.

THE ALL-OR-NOTHING SWITCH

Everything here is gated on ``omnigraph:keycloak_url`` being set for the
environment, because turning it on also transfers ownership of the
actor-tokens Vault path from Pulumi to this job (see ``__main__.py``). An
environment that has not had its Keycloak client provisioned yet keeps the
SOPS-driven behaviour unchanged rather than getting a CronJob that fails every
interval on a missing credential.
"""

import hashlib
from pathlib import Path
from typing import NamedTuple

import pulumi_kubernetes as kubernetes
from pulumi import Output, Resource, ResourceOptions
from pulumi_vault import Policy
from pulumi_vault import kubernetes as vault_kubernetes

from ol_infrastructure.components.services.vault import (
    OLVaultK8SSecret,
    OLVaultK8SStaticSecretConfig,
)
from ol_infrastructure.lib.aws.eks_helper import cached_image_uri
from ol_infrastructure.lib.pulumi_helper import StackInfo

# Stock upstream image, pulled through the account's Docker Hub cache rather
# than from Docker Hub directly — the same accommodation vector_log_proxy makes,
# and the reason a rate-limited anonymous pull cannot wedge this job.
TOKEN_SYNC_IMAGE = cached_image_uri("python:3.12-slim")

SERVICE_ACCOUNT_NAME = "witan-token-sync"
VAULT_ROLE_NAME = "witan-token-sync"
SCRIPT_MOUNT_PATH = "/opt/witan-token-sync"
SCRIPT_FILENAME = "sync_actor_tokens.py"

# The Vault paths this job reads and writes. Both are the kv-v1 form used
# everywhere in this stack, and both are relative to the same `secret-operations`
# mount; they are spelled out in full here because they also appear verbatim in
# token_sync_policy.hcl and a mismatch between the two is a 403 at runtime.
ACTOR_TOKENS_VAULT_PATH = (  # pragma: allowlist secret
    "secret-operations/witan/actor-tokens"
)
SERVICE_TOKENS_VAULT_PATH = (  # pragma: allowlist secret
    "secret-operations/witan/service-tokens"
)

# Where the keycloak substructure stack publishes the token-sync service
# account's OIDC credentials. Filed under `witan/` with the rest of this
# deployment's secrets rather than under the `secret-operations/sso/<app>`
# convention the realm's other clients use: those are per-application login
# credentials consumed by the application itself, whereas this is one more
# piece of witan's own plumbing, and grouping it with witan/actor-tokens and
# witan/ci-token is what lets one policy stanza per path stay readable.
# Relative to the `secret-operations` mount, as OLVaultK8SStaticSecretConfig
# expects — the full path also appears in omnigraph_policy.hcl.
KEYCLOAK_CREDENTIALS_VAULT_PATH = "witan/token-sync-oidc"
KEYCLOAK_CREDENTIALS_SECRET_NAME = "witan-token-sync-oidc"  # noqa: S105  # pragma: allowlist secret

# Hourly. The floor on this is not Keycloak's cost — the job makes two API
# calls — but the fact that a membership change writes Vault and therefore
# bounces omnigraph-server (replicas=1/Recreate, a hard ~10-30s graph outage
# absorbed by client-side connect retry). Hourly makes onboarding latency an
# hour in the worst case while keeping any conceivable churn well clear of
# overlapping restarts. Steady state is free: an unchanged membership writes
# nothing at all, so the restart cost is paid only on real change.
DEFAULT_SYNC_SCHEDULE = "17 * * * *"

# Two HTTPS calls and a Vault write. A run that has not finished in five
# minutes is wedged on a hung connection, not slow.
SYNC_ACTIVE_DEADLINE_SECONDS = 300

# The script is idempotent, so a retry is safe; two of them ride out a Keycloak
# blip or an omnigraph-server rollout without waiting for the next hour.
SYNC_BACKOFF_LIMIT = 2


def _pod_spec(  # noqa: PLR0913
    script_config_map_name: str | Output[str],
    keycloak_url: str,
    keycloak_realm: str,
    vault_address: str,
    vault_auth_endpoint: str | Output[str],
    k8s_global_labels: dict[str, str],
) -> kubernetes.core.v1.PodTemplateSpecArgs:
    """Build the pod both the CronJob and the bootstrap Job run.

    Shared rather than duplicated because the two must stay identical: the
    bootstrap run exists to make the scheduled run's first output available
    before anything depends on it, and a bootstrap that ran a different
    configuration would defeat that.
    """
    return kubernetes.core.v1.PodTemplateSpecArgs(
        metadata=kubernetes.meta.v1.ObjectMetaArgs(
            labels={
                **k8s_global_labels,
                "app.kubernetes.io/name": SERVICE_ACCOUNT_NAME,
            },
        ),
        spec=kubernetes.core.v1.PodSpecArgs(
            restart_policy="Never",
            service_account_name=SERVICE_ACCOUNT_NAME,
            # Required, and the one place in this deployment where it is: the
            # projected ServiceAccount token IS the credential the script
            # presents to Vault's Kubernetes auth method. The sibling CI
            # indexer sets this False precisely because it has no such need.
            automount_service_account_token=True,
            security_context=kubernetes.core.v1.PodSecurityContextArgs(
                run_as_non_root=True,
                run_as_user=1000,
                run_as_group=1000,
            ),
            containers=[
                kubernetes.core.v1.ContainerArgs(
                    name="sync-actor-tokens",
                    image=TOKEN_SYNC_IMAGE,
                    command=["python", f"{SCRIPT_MOUNT_PATH}/{SCRIPT_FILENAME}"],
                    env=[
                        kubernetes.core.v1.EnvVarArgs(
                            name="VAULT_ADDR", value=vault_address
                        ),
                        kubernetes.core.v1.EnvVarArgs(
                            name="VAULT_K8S_AUTH_MOUNT", value=vault_auth_endpoint
                        ),
                        kubernetes.core.v1.EnvVarArgs(
                            name="VAULT_K8S_ROLE", value=VAULT_ROLE_NAME
                        ),
                        kubernetes.core.v1.EnvVarArgs(
                            name="ACTOR_TOKENS_VAULT_PATH",
                            value=ACTOR_TOKENS_VAULT_PATH,
                        ),
                        kubernetes.core.v1.EnvVarArgs(
                            name="SERVICE_TOKENS_VAULT_PATH",
                            value=SERVICE_TOKENS_VAULT_PATH,
                        ),
                        kubernetes.core.v1.EnvVarArgs(
                            name="KEYCLOAK_URL", value=keycloak_url
                        ),
                        kubernetes.core.v1.EnvVarArgs(
                            name="KEYCLOAK_REALM", value=keycloak_realm
                        ),
                        kubernetes.core.v1.EnvVarArgs(
                            name="KEYCLOAK_CLIENT_ID",
                            value_from=kubernetes.core.v1.EnvVarSourceArgs(
                                secret_key_ref=kubernetes.core.v1.SecretKeySelectorArgs(
                                    name=KEYCLOAK_CREDENTIALS_SECRET_NAME,
                                    key="client_id",
                                )
                            ),
                        ),
                        kubernetes.core.v1.EnvVarArgs(
                            name="KEYCLOAK_CLIENT_SECRET",
                            value_from=kubernetes.core.v1.EnvVarSourceArgs(
                                secret_key_ref=kubernetes.core.v1.SecretKeySelectorArgs(
                                    name=KEYCLOAK_CREDENTIALS_SECRET_NAME,
                                    key="client_secret",
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
                    # A stdlib script making two HTTPS calls. The limits are
                    # here to make it evictable-last rather than because it is
                    # anywhere near them.
                    resources=kubernetes.core.v1.ResourceRequirementsArgs(
                        requests={"cpu": "50m", "memory": "64Mi"},
                        limits={"cpu": "500m", "memory": "256Mi"},
                    ),
                )
            ],
            volumes=[
                kubernetes.core.v1.VolumeArgs(
                    name="script",
                    config_map=kubernetes.core.v1.ConfigMapVolumeSourceArgs(
                        name=script_config_map_name,
                        default_mode=0o555,
                    ),
                )
            ],
        ),
    )


class WitanTokenSync(NamedTuple):
    """Handles to the provisioned token-sync resources for depends_on wiring."""

    cron_job: kubernetes.batch.v1.CronJob
    bootstrap_job: kubernetes.batch.v1.Job


def create_token_sync(  # noqa: PLR0913
    stack_info: StackInfo,
    namespace: str,
    k8s_global_labels: dict[str, str],
    keycloak_url: str,
    keycloak_realm: str,
    vault_address: str,
    vault_auth_endpoint: str | Output[str],
    vault_auth_name: str,
    schedule: str,
    depends_on: list[Resource],
) -> WitanTokenSync:
    """Provision the identity, credentials, schedule and bootstrap run.

    ``bootstrap_job`` on the result is what callers should order the
    actor-tokens VSO sync behind — see ``__main__.py``, which threads it in so
    that a brand-new environment has a written actor-tokens path before
    anything tries to render it.
    """
    script_body = (Path(__file__).parent / "scripts" / SCRIPT_FILENAME).read_text()

    # Its own ServiceAccount rather than the namespace's existing two. The IRSA
    # one carries S3 write access to the graph bucket and the VSO one carries
    # system:auth-delegator; this pod needs neither, and reusing either would
    # hand it a credential far broader than the single Vault write it makes.
    service_account = kubernetes.core.v1.ServiceAccount(
        f"witan-token-sync-service-account-{stack_info.env_suffix}",
        metadata=kubernetes.meta.v1.ObjectMetaArgs(
            name=SERVICE_ACCOUNT_NAME,
            namespace=namespace,
            labels=k8s_global_labels,
        ),
    )

    vault_policy = Policy(
        f"witan-token-sync-vault-policy-{stack_info.env_suffix}",
        name=VAULT_ROLE_NAME,
        policy=(Path(__file__).parent / "token_sync_policy.hcl").read_text(),
    )

    vault_auth_role = vault_kubernetes.AuthBackendRole(
        f"witan-token-sync-k8s-vault-auth-backend-role-{stack_info.env_suffix}",
        role_name=VAULT_ROLE_NAME,
        backend=vault_auth_endpoint,
        bound_service_account_names=[SERVICE_ACCOUNT_NAME],
        bound_service_account_namespaces=[namespace],
        token_policies=[vault_policy.name],
        opts=ResourceOptions(depends_on=[vault_policy]),
    )

    # The Keycloak service-account credentials, written to Vault by the
    # keycloak substructure stack. Synced with the namespace's existing VaultAuth
    # (read-only, omnigraph_policy.hcl) rather than this job's write-capable
    # role: the operator only ever reads it, and the write capability exists for
    # the pod, not for the sync.
    keycloak_credentials_secret = OLVaultK8SSecret(
        f"witan-token-sync-oidc-secret-{stack_info.env_suffix}",
        resource_config=OLVaultK8SStaticSecretConfig(
            name=KEYCLOAK_CREDENTIALS_SECRET_NAME,
            namespace=namespace,
            labels=k8s_global_labels,
            dest_secret_labels=k8s_global_labels,
            dest_secret_name=KEYCLOAK_CREDENTIALS_SECRET_NAME,
            dest_secret_type="Opaque",  # pragma: allowlist secret  # noqa: S106
            mount="secret-operations",
            mount_type="kv-v1",
            path=KEYCLOAK_CREDENTIALS_VAULT_PATH,
            exclude_raw=True,
            excludes=[".*"],
            templates={
                "client_id": '{{ get .Secrets "client_id" }}',
                "client_secret": '{{ get .Secrets "client_secret" }}',
            },
            refresh_after="1h",
            # No restart target: the credential is read once per run by a pod
            # that exits, so the next run picks up a rotation on its own.
            vaultauth=vault_auth_name,
        ),
        opts=ResourceOptions(delete_before_replace=True, depends_on=depends_on),
    )

    # Auto-named so a script change rolls a new ConfigMap rather than mutating
    # one in place: a mutated ConfigMap propagates to a running pod on the
    # kubelet's own schedule, which for a CronJob means an indeterminate mix of
    # old and new for the next interval.
    script_config_map = kubernetes.core.v1.ConfigMap(
        f"witan-token-sync-script-{stack_info.env_suffix}",
        metadata=kubernetes.meta.v1.ObjectMetaArgs(
            namespace=namespace,
            labels=k8s_global_labels,
        ),
        data={SCRIPT_FILENAME: script_body},
    )

    pod_template = _pod_spec(
        script_config_map_name=script_config_map.metadata.name,
        keycloak_url=keycloak_url,
        keycloak_realm=keycloak_realm,
        vault_address=vault_address,
        vault_auth_endpoint=vault_auth_endpoint,
        k8s_global_labels=k8s_global_labels,
    )

    job_depends_on: list[Resource] = [
        service_account,
        vault_auth_role,
        keycloak_credentials_secret,
        script_config_map,
    ]

    # Runs the same reconciliation once at deploy time. Without it a brand-new
    # environment has no actor-tokens path at all until the first scheduled
    # tick, and the VSO sync that renders it — and the Deployment that mounts
    # the result — would come up against nothing. Pulumi waits for a Job to
    # succeed, so this also makes a broken Keycloak credential fail the deploy
    # loudly instead of producing a CronJob that silently errors every hour.
    bootstrap_hash = hashlib.sha256(
        f"{script_body}\n{keycloak_url}\n{keycloak_realm}".encode()
    ).hexdigest()
    bootstrap_job = kubernetes.batch.v1.Job(
        f"witan-token-sync-bootstrap-{stack_info.env_suffix}",
        # Auto-named for the same reason as cluster_apply_job: a Job's pod
        # template is immutable, so every change is a replacement.
        metadata=kubernetes.meta.v1.ObjectMetaArgs(
            namespace=namespace,
            labels=k8s_global_labels,
        ),
        spec=kubernetes.batch.v1.JobSpecArgs(
            backoff_limit=SYNC_BACKOFF_LIMIT,
            active_deadline_seconds=SYNC_ACTIVE_DEADLINE_SECONDS,
            ttl_seconds_after_finished=86400,
            template=kubernetes.core.v1.PodTemplateSpecArgs(
                metadata=kubernetes.meta.v1.ObjectMetaArgs(
                    labels=pod_template.metadata.labels,
                    # Re-runs the bootstrap when the script or the Keycloak
                    # target changes. Membership changes deliberately do NOT
                    # roll this — that is the CronJob's job, and a deploy is not
                    # where onboarding should happen.
                    annotations={"ol.mit.edu/config-hash": bootstrap_hash},
                ),
                spec=pod_template.spec,
            ),
        ),
        opts=ResourceOptions(depends_on=job_depends_on),
    )

    cron_job = kubernetes.batch.v1.CronJob(
        f"witan-token-sync-{stack_info.env_suffix}",
        metadata=kubernetes.meta.v1.ObjectMetaArgs(
            name=SERVICE_ACCOUNT_NAME,
            namespace=namespace,
            labels=k8s_global_labels,
        ),
        spec=kubernetes.batch.v1.CronJobSpecArgs(
            schedule=schedule,
            # Two concurrent runs would read the same actor-tokens map and
            # write back divergent merges, and the loser's freshly minted
            # tokens would be silently dropped. The reconciliation is a
            # read-modify-write with no compare-and-swap behind it, so
            # serialising the runs is what stands in for one.
            concurrency_policy="Forbid",
            starting_deadline_seconds=600,
            successful_jobs_history_limit=1,
            # A failed run is the one whose logs somebody needs; onboarding
            # silently not happening is exactly the symptom that sends an
            # operator here.
            failed_jobs_history_limit=3,
            job_template=kubernetes.batch.v1.JobTemplateSpecArgs(
                metadata=kubernetes.meta.v1.ObjectMetaArgs(labels=k8s_global_labels),
                spec=kubernetes.batch.v1.JobSpecArgs(
                    backoff_limit=SYNC_BACKOFF_LIMIT,
                    active_deadline_seconds=SYNC_ACTIVE_DEADLINE_SECONDS,
                    template=pod_template,
                ),
            ),
        ),
        opts=ResourceOptions(depends_on=[*job_depends_on, bootstrap_job]),
    )

    return WitanTokenSync(cron_job=cron_job, bootstrap_job=bootstrap_job)
