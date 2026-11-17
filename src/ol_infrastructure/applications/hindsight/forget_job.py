"""Deploy the job that forgets the memory of learners deleted from Keycloak.

The job itself is ``scripts/forget_deleted_learners.py``; its docstring covers what
a run does and the guards on it. This module is the deployment half.

The forget is platform-owned (decided 2026-11-13): no app holds the platform admin
client, because a leaked admin secret can wipe any learner's shared memory and
recovery is a whole-database restore. Only this job's pods get it, through a Vault
role of their own that reads the admin client, the Keycloak lookup client and a
database login that sees bank ids and nothing else. The Hindsight API's Vault role
is unchanged.

Learner banks are listed from the database rather than the API because the admin
client lists no banks (``ol_memory_ext.LearnerBankValidator.bank_list_scope``) and
must name one learner in every request. Giving it a list permission would widen
what a leaked admin secret can do. The ``bank-ids`` database role can select
``public.banks.bank_id`` and no other column or table, so the job never reads a
note.

The script runs on the stock uv image with its dependencies pinned in the
adjacent lockfile (``uv lock --script``), so ol-llm's tested forget sequence is
used as-is without an image to build. ``--locked`` makes a lockfile that no
longer matches the script fail the run instead of resolving afresh.
"""

from pathlib import Path
from string import Template

import pulumi_kubernetes as kubernetes
from pulumi import Output, ResourceOptions
from pulumi_vault import Policy
from pulumi_vault import kubernetes as vault_kubernetes

from bridge.lib.magic_numbers import DEFAULT_POSTGRES_PORT
from bridge.lib.versions import UV_IMAGE_TAG
from ol_infrastructure.components.services.vault import (
    OLVaultK8SDynamicSecretConfig,
    OLVaultK8SResources,
    OLVaultK8SResourcesConfig,
    OLVaultK8SSecret,
    OLVaultK8SStaticSecretConfig,
)
from ol_infrastructure.lib.ol_types import AlertTier, Component, K8sAppLabels
from ol_infrastructure.lib.pulumi_helper import StackInfo

NAME = "hindsight-forget"
SCRIPT_DIR = Path(__file__).parent / "scripts"
SCRIPT_FILENAME = "forget_deleted_learners.py"
LOCK_FILENAME = f"{SCRIPT_FILENAME}.lock"
SCRIPT_MOUNT_PATH = "/opt/hindsight-forget"
SCRATCH_PATH = "/tmp"  # noqa: S108
BANK_IDS_DB_ROLE = "bank-ids"
# The forget path's span attribution; the service knows this caller as the
# platform admin, not as an app.
ADMIN_APP = "platform-admin"
ADMIN_CLIENT_VAULT_PATH = "sso/ol-memory-admin"
LOOKUP_CLIENT_VAULT_PATH = "sso/ol-memory-forget-lookup"
# Each forget waits for the bank's in-flight work to drain, so a run at the cap
# can take minutes. An hour means a hung connection, not a slow run.
ACTIVE_DEADLINE_SECONDS = 3600

# A login that can read bank ids and nothing else. The tables belong to the app
# role, so the grant is made as that role, the way the app role's own statements
# do. Revoking the table privilege also revokes the column privilege.
bank_ids_role_statements = {
    "create": [
        Template(
            """
            CREATE USER "{{name}}" WITH PASSWORD '{{password}}'
            VALID UNTIL '{{expiration}}';
            """
        ),
        Template("""SET ROLE "${app_name}";"""),
        Template("""GRANT SELECT (bank_id) ON public.banks TO "{{name}}";"""),
        Template("""RESET ROLE;"""),
    ],
    "revoke": [
        Template("""SET ROLE "${app_name}";"""),
        Template("""REVOKE ALL PRIVILEGES ON public.banks FROM "{{name}}";"""),
        Template("""RESET ROLE;"""),
        Template("""DROP USER "{{name}}";"""),
    ],
    "renew": [],
    "rollback": [],
}


def create_forget_job(  # noqa: PLR0913
    *,
    stack_info: StackInfo,
    namespace: str,
    k8s_app_labels: K8sAppLabels,
    alert_tier: AlertTier,
    vault_address: str,
    vault_auth_endpoint: str | Output[str],
    db_mount: str | Output[str],
    db_address: Output[str],
    db_name: str,
    hindsight_url: str,
    keycloak_issuer: str,
    schedule: str,
    forget_cap: int,
) -> kubernetes.batch.v1.CronJob:
    """Provision the job's Vault identity, credentials, script and schedule.

    :param stack_info: The stack being deployed.
    :param namespace: The Hindsight namespace.
    :param k8s_app_labels: The Hindsight stack's labels.
    :param alert_tier: How far a failed run's alert may escalate.
    :param vault_address: The environment's Vault.
    :param vault_auth_endpoint: The cluster's Vault Kubernetes auth mount.
    :param db_mount: The Hindsight database's Vault secrets mount.
    :param db_address: The Hindsight database's host.
    :param db_name: The Hindsight database's name.
    :param hindsight_url: The Hindsight API, in-cluster.
    :param keycloak_issuer: The olapps realm's issuer URL.
    :param schedule: The cron schedule.
    :param forget_cap: The most learners one run may forget.
    :returns: The CronJob.
    :rtype: kubernetes.batch.v1.CronJob
    """
    labels = k8s_app_labels.model_dump()
    keycloak_url, realm = keycloak_issuer.rsplit("/realms/", 1)

    vault_policy = Policy(
        f"{NAME}-vault-policy-{stack_info.env_suffix}",
        name=NAME,
        policy=(Path(__file__).parent / "forget_policy.hcl").read_text(),
    )
    vault_auth_role = vault_kubernetes.AuthBackendRole(
        f"{NAME}-k8s-vault-auth-backend-role-{stack_info.env_suffix}",
        role_name=NAME,
        backend=vault_auth_endpoint,
        bound_service_account_names=[f"{NAME}-vault"],
        bound_service_account_namespaces=[namespace],
        token_policies=[vault_policy.name],
    )
    vault_k8s_resources = OLVaultK8SResources(
        OLVaultK8SResourcesConfig(
            application_name=NAME,
            namespace=namespace,
            labels=labels,
            vault_address=vault_address,
            vault_auth_endpoint=vault_auth_endpoint,
            vault_auth_role_name=vault_auth_role.role_name,
        ),
        opts=ResourceOptions(delete_before_replace=True, depends_on=[vault_auth_role]),
    )

    def static_secret(
        resource_name: str, path: str, prefix: str
    ) -> tuple[str, OLVaultK8SSecret]:
        secret_name = f"{NAME}-{resource_name}"
        secret = OLVaultK8SSecret(
            f"{secret_name}-{stack_info.env_suffix}",
            OLVaultK8SStaticSecretConfig(
                name=secret_name,
                namespace=namespace,
                labels=labels,
                dest_secret_labels=labels,
                dest_secret_name=secret_name,
                mount="secret-operations",
                mount_type="kv-v1",
                path=path,
                templates={
                    f"{prefix}_CLIENT_ID": '{{ get .Secrets "client_id" }}',
                    f"{prefix}_CLIENT_SECRET": '{{ get .Secrets "client_secret" }}',
                },
                refresh_after="1h",
                vaultauth=vault_k8s_resources.auth_name,
            ),
            opts=ResourceOptions(
                delete_before_replace=True, depends_on=[vault_k8s_resources]
            ),
        )
        return secret_name, secret

    admin_secret_name, admin_secret = static_secret(
        "admin-client", ADMIN_CLIENT_VAULT_PATH, "OL_LLM_MEMORY_ADMIN"
    )
    lookup_secret_name, lookup_secret = static_secret(
        "lookup-client", LOOKUP_CLIENT_VAULT_PATH, "KEYCLOAK_LOOKUP"
    )
    db_secret_name = f"{NAME}-db-creds"
    db_secret = OLVaultK8SSecret(
        f"{db_secret_name}-{stack_info.env_suffix}",
        OLVaultK8SDynamicSecretConfig(
            name=db_secret_name,
            namespace=namespace,
            labels=labels,
            dest_secret_labels=labels,
            dest_secret_name=db_secret_name,
            mount=db_mount,
            path=f"creds/{BANK_IDS_DB_ROLE}",
            exclude_raw=True,
            templates={
                "DB_USERNAME": '{{ get .Secrets "username" }}',
                "DB_PASSWORD": '{{ get .Secrets "password" }}',
            },
            revoke_on_delete=True,
            vaultauth=vault_k8s_resources.auth_name,
        ),
        opts=ResourceOptions(
            delete_before_replace=True, depends_on=[vault_k8s_resources]
        ),
    )

    # Its own account: the API's carries the Bedrock IRSA role, which this job
    # has no use for. It reaches Vault only through the operator.
    service_account = kubernetes.core.v1.ServiceAccount(
        f"{NAME}-service-account-{stack_info.env_suffix}",
        metadata=kubernetes.meta.v1.ObjectMetaArgs(
            name=NAME, namespace=namespace, labels=labels
        ),
        automount_service_account_token=False,
    )

    # Auto-named and replaced on change, so a run never mounts a half-updated
    # script and lockfile pair.
    script_config_map = kubernetes.core.v1.ConfigMap(
        f"{NAME}-script-{stack_info.env_suffix}",
        metadata=kubernetes.meta.v1.ObjectMetaArgs(namespace=namespace, labels=labels),
        data={
            name: (SCRIPT_DIR / name).read_text()
            for name in (SCRIPT_FILENAME, LOCK_FILENAME)
        },
        opts=ResourceOptions(replace_on_changes=["data"], delete_before_replace=False),
    )

    env = {
        "FORGET_CAP": str(forget_cap),
        "KEYCLOAK_URL": keycloak_url,
        "KEYCLOAK_REALM": realm,
        "DB_HOST": db_address,
        "DB_PORT": str(DEFAULT_POSTGRES_PORT),
        "DB_NAME": db_name,
        "OL_LLM_MEMORY_BACKEND": "hindsight",
        "OL_LLM_MEMORY_APP": ADMIN_APP,
        "OL_LLM_MEMORY_URL": hindsight_url,
        "OL_LLM_MEMORY_TOKEN_URL": f"{keycloak_issuer}/protocol/openid-connect/token",
        # The forget span carries the learner id, and there is no trace
        # destination this job should send it to.
        "OPIK_TRACK_DISABLE": "true",
        "HOME": SCRATCH_PATH,
        "UV_CACHE_DIR": f"{SCRATCH_PATH}/uv-cache",
        "UV_NO_CONFIG": "true",
        "UV_PYTHON_DOWNLOADS": "never",
    }
    pod_labels = {
        **k8s_app_labels.workload_labels(
            alert_tier=alert_tier, component=Component.worker
        ),
        "app.kubernetes.io/name": NAME,
    }
    pod_template = kubernetes.core.v1.PodTemplateSpecArgs(
        metadata=kubernetes.meta.v1.ObjectMetaArgs(labels=pod_labels),
        spec=kubernetes.core.v1.PodSpecArgs(
            restart_policy="Never",
            service_account_name=NAME,
            automount_service_account_token=False,
            security_context=kubernetes.core.v1.PodSecurityContextArgs(
                run_as_non_root=True, run_as_user=1000, run_as_group=1000
            ),
            containers=[
                kubernetes.core.v1.ContainerArgs(
                    name=NAME,
                    image=f"ghcr.io/astral-sh/uv:{UV_IMAGE_TAG}",
                    command=[
                        "uv",
                        "run",
                        "--locked",
                        "--script",
                        f"{SCRIPT_MOUNT_PATH}/{SCRIPT_FILENAME}",
                    ],
                    env=[
                        kubernetes.core.v1.EnvVarArgs(name=name, value=value)
                        for name, value in env.items()
                    ],
                    env_from=[
                        kubernetes.core.v1.EnvFromSourceArgs(
                            secret_ref=kubernetes.core.v1.SecretEnvSourceArgs(
                                name=secret_name
                            )
                        )
                        for secret_name in (
                            admin_secret_name,
                            lookup_secret_name,
                            db_secret_name,
                        )
                    ],
                    security_context=kubernetes.core.v1.SecurityContextArgs(
                        allow_privilege_escalation=False,
                        read_only_root_filesystem=True,
                    ),
                    resources=kubernetes.core.v1.ResourceRequirementsArgs(
                        requests={"cpu": "100m", "memory": "256Mi"},
                        limits={"memory": "512Mi"},
                    ),
                    volume_mounts=[
                        kubernetes.core.v1.VolumeMountArgs(
                            name="script", mount_path=SCRIPT_MOUNT_PATH, read_only=True
                        ),
                        kubernetes.core.v1.VolumeMountArgs(
                            name="scratch", mount_path=SCRATCH_PATH
                        ),
                    ],
                )
            ],
            volumes=[
                kubernetes.core.v1.VolumeArgs(
                    name="script",
                    config_map=kubernetes.core.v1.ConfigMapVolumeSourceArgs(
                        name=script_config_map.metadata.name
                    ),
                ),
                kubernetes.core.v1.VolumeArgs(
                    name="scratch",
                    empty_dir=kubernetes.core.v1.EmptyDirVolumeSourceArgs(
                        size_limit="1Gi"
                    ),
                ),
            ],
        ),
    )

    return kubernetes.batch.v1.CronJob(
        f"{NAME}-cronjob-{stack_info.env_suffix}",
        metadata=kubernetes.meta.v1.ObjectMetaArgs(
            name=NAME, namespace=namespace, labels=labels
        ),
        spec=kubernetes.batch.v1.CronJobSpecArgs(
            schedule=schedule,
            # Two runs would forget the same learners at once. The sequence is
            # idempotent, but there is nothing to gain.
            concurrency_policy="Forbid",
            starting_deadline_seconds=3600,
            successful_jobs_history_limit=1,
            failed_jobs_history_limit=3,
            job_template=kubernetes.batch.v1.JobTemplateSpecArgs(
                metadata=kubernetes.meta.v1.ObjectMetaArgs(labels=labels),
                spec=kubernetes.batch.v1.JobSpecArgs(
                    # One retry rides out a Keycloak or Hindsight blip. A run that
                    # hit the cap fails again and the Job fails, which alerts
                    # (WorkloadJobFailed* in grafana_alerting).
                    backoff_limit=1,
                    active_deadline_seconds=ACTIVE_DEADLINE_SECONDS,
                    template=pod_template,
                ),
            ),
        ),
        opts=ResourceOptions(
            depends_on=[service_account, admin_secret, lookup_secret, db_secret]
        ),
    )
