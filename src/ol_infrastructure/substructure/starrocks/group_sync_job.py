"""Run the Keycloak -> StarRocks group sync on a schedule.

The Pulumi-time sync (``group_sync_cmd`` in ``__main__.py``) only re-runs when its
script or the integration SQL changes, and the FE reads groups.txt only when the
group provider is created or altered. Before this job, a Keycloak role change took
effect at the next FE restart.

Each run has two steps in one pod:

1. ``sync`` (init container, stdlib Python): ``keycloak_group_sync.py --in-cluster``
   patches the ConfigMap if the content changed and drops a marker file when it
   did. It is the only container with a ServiceAccount token, and that token can
   only get and patch this one ConfigMap.
2. ``reload`` (mysql client): if the marker exists, wait for kubelet to project
   the new file into the FE pods, then run a no-op ``ALTER GROUP PROVIDER``.

On StarRocks 4.1.7+ that ALTER re-reads the file: the leader runs init() on the
new provider, and every follower replays the journaled ALTER by running init()
against its own local copy (AuthenticationMgr.alterGroupProvider and
replayAlterGroupProvider). A follower whose kubelet had not projected the file yet
reads the old one, so the ALTER runs on every run, not only after a change: a
lagging follower catches up on the next run.

ALTER GROUP PROVIDER needs SECURITY on SYSTEM, which also covers security
integrations. The credential is a Vault dynamic user holding only the
group_provider_admin role, and only the reload container sees it.

An empty Keycloak result would refuse every OIDC login within one run, so the sync
refuses to replace a non-empty file with an empty one (see
write_configmap_in_cluster). The job then fails and alerts instead.

Superseded at StarRocks 4.2 by group_source=claim on both security integrations
(StarRocks#80942), which takes groups from the token and removes this job, the
sync script, the ConfigMap and the group provider.
"""

from pathlib import Path

import pulumi_kubernetes as kubernetes
from pulumi import Input, Resource, ResourceOptions

from ol_infrastructure.components.services.vault import (
    OLVaultK8SDynamicSecretConfig,
    OLVaultK8SSecret,
)
from ol_infrastructure.lib.aws.eks_helper import cached_image_uri
from ol_infrastructure.lib.pulumi_helper import StackInfo

SYNC_IMAGE = cached_image_uri("python:3.12-slim")
# Only the mysql client is used. The 8.4 LTS line, with no openid plugin needed:
# the Vault user authenticates with a password.
RELOAD_IMAGE = cached_image_uri("mysql:8.4")

GROUP_SYNC_NAME = "starrocks-group-sync"
# Keep it in the fast bucket of the ScheduledJobStale rules in
# infrastructure/grafana_alerting/metric_rules/eks_general.py.
GROUP_SYNC_SCHEDULE = "*/15 * * * *"
# kubelet re-projects a ConfigMap volume on its pod sync, every syncFrequency (1m
# by default) plus jitter. A follower that is still behind catches up on the next
# run, so this bounds the common case rather than guaranteeing it.
KUBELET_PROJECTION_WAIT_SECONDS = 120
# One Keycloak request per realm user, plus the wait above.
GROUP_SYNC_ACTIVE_DEADLINE_SECONDS = 600

GROUP_PROVIDER_ADMIN_ROLE = "group_provider_admin"
CREDENTIALS_SECRET_NAME = f"{GROUP_SYNC_NAME}-starrocks"  # pragma: allowlist secret
SCRIPT_MOUNT_PATH = "/opt/starrocks-group-sync"
SCRIPT_FILENAME = "keycloak_group_sync.py"
WORK_MOUNT_PATH = "/work"
CHANGED_FILE = f"{WORK_MOUNT_PATH}/changed"
SERVICE_ACCOUNT_MOUNT_PATH = "/var/run/secrets/kubernetes.io/serviceaccount"
SERVICE_ACCOUNT_TOKEN_SECONDS = 600

_RESTRICTED_SECURITY_CONTEXT = kubernetes.core.v1.SecurityContextArgs(
    run_as_non_root=True,
    run_as_user=1000,
    run_as_group=1000,
    allow_privilege_escalation=False,
    read_only_root_filesystem=True,
    capabilities=kubernetes.core.v1.CapabilitiesArgs(drop=["ALL"]),
)
_RESOURCES = kubernetes.core.v1.ResourceRequirementsArgs(
    requests={"cpu": "25m", "memory": "64Mi"},
    limits={"cpu": "250m", "memory": "128Mi"},
)


def reload_command(
    group_provider: str, group_file_path: str, mysql_port: int, *, ssl_enabled: bool
) -> str:
    """Build the reload container's shell command.

    :param group_provider: Name of the file group provider.
    :param group_file_path: Its group_file_url, re-set unchanged.
    :param mysql_port: The FE query port.
    :param ssl_enabled: Whether the FE requires TLS on the query port.
    :returns: A script for ``sh -c``.
    :rtype: str
    """
    ssl_mode = "REQUIRED" if ssl_enabled else "DISABLED"
    alter_sql = (
        f"ALTER GROUP PROVIDER {group_provider} "
        f'SET ("group_file_url" = "{group_file_path}");'
    )
    return (
        "set -eu\n"
        f"if [ -e {CHANGED_FILE} ]; then\n"
        f'  echo "groups.txt changed; waiting {KUBELET_PROJECTION_WAIT_SECONDS}s'
        ' for kubelet to project it into the FE pods"\n'
        f"  sleep {KUBELET_PROJECTION_WAIT_SECONDS}\n"
        "fi\n"
        f'mysql -h"$STARROCKS_HOST" -P{mysql_port} -u"$STARROCKS_USER"'
        f" --ssl-mode={ssl_mode} -e '{alter_sql}'\n"
        f'echo "Reloaded group provider {group_provider}"\n'
    )


def create_group_sync_job(  # noqa: PLR0913
    stack_info: StackInfo,
    namespace: str,
    k8s_labels: dict[str, str],
    *,
    configmap_name: str,
    group_provider: str,
    group_file_path: str,
    fe_host: Input[str],
    mysql_port: int,
    ssl_enabled: bool,
    vault_mount: Input[str],
    vault_auth_name: str,
    oidc_secret_name: str,
    depends_on: list[Resource],
) -> kubernetes.batch.v1.CronJob:
    """Provision the scheduled group sync.

    :param configmap_name: The ConfigMap the FE pods mount groups.txt from.
    :param group_provider: Name of the file group provider to reload.
    :param group_file_path: The provider's group_file_url.
    :param fe_host: In-cluster FE service hostname.
    :param mysql_port: The FE query port.
    :param ssl_enabled: Whether the FE requires TLS on the query port.
    :param vault_mount: The StarRocks database secrets mount.
    :param vault_auth_name: The VaultAuth in the namespace.
    :param oidc_secret_name: The VSO-synced Secret holding the ol-starrocks-client
        credentials, created by the applications stack.
    :param depends_on: Resources the first scheduled run needs in place.
    :returns: The CronJob.
    :rtype: kubernetes.batch.v1.CronJob
    """
    suffix = stack_info.env_suffix
    script_body = (Path(__file__).parent / SCRIPT_FILENAME).read_text()

    script_config_map = kubernetes.core.v1.ConfigMap(
        f"{GROUP_SYNC_NAME}-script-{suffix}",
        metadata=kubernetes.meta.v1.ObjectMetaArgs(
            namespace=namespace,
            labels=k8s_labels,
        ),
        data={SCRIPT_FILENAME: script_body},
        opts=ResourceOptions(replace_on_changes=["data"], delete_before_replace=False),
    )

    service_account = kubernetes.core.v1.ServiceAccount(
        f"{GROUP_SYNC_NAME}-service-account-{suffix}",
        metadata=kubernetes.meta.v1.ObjectMetaArgs(
            name=GROUP_SYNC_NAME,
            namespace=namespace,
            labels=k8s_labels,
        ),
        automount_service_account_token=False,
    )
    role = kubernetes.rbac.v1.Role(
        f"{GROUP_SYNC_NAME}-role-{suffix}",
        metadata=kubernetes.meta.v1.ObjectMetaArgs(
            name=GROUP_SYNC_NAME,
            namespace=namespace,
            labels=k8s_labels,
        ),
        rules=[
            kubernetes.rbac.v1.PolicyRuleArgs(
                api_groups=[""],
                resources=["configmaps"],
                resource_names=[configmap_name],
                verbs=["get", "patch"],
            )
        ],
    )
    role_binding = kubernetes.rbac.v1.RoleBinding(
        f"{GROUP_SYNC_NAME}-role-binding-{suffix}",
        metadata=kubernetes.meta.v1.ObjectMetaArgs(
            name=GROUP_SYNC_NAME,
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

    credentials = OLVaultK8SSecret(
        f"{GROUP_SYNC_NAME}-credentials-{suffix}",
        OLVaultK8SDynamicSecretConfig(
            name=CREDENTIALS_SECRET_NAME,
            namespace=namespace,
            labels=k8s_labels,
            dest_secret_labels=k8s_labels,
            dest_secret_name=CREDENTIALS_SECRET_NAME,
            exclude_raw=True,
            mount=vault_mount,
            path=f"creds/{GROUP_PROVIDER_ADMIN_ROLE}",
            templates={
                "STARROCKS_USER": '{{ get .Secrets "username" }}',
                "MYSQL_PWD": '{{ get .Secrets "password" }}',
            },
            vaultauth=vault_auth_name,
            revoke_on_delete=True,
        ),
        opts=ResourceOptions(delete_before_replace=True, depends_on=depends_on),
    )

    def _oidc_env(name: str, key: str) -> kubernetes.core.v1.EnvVarArgs:
        return kubernetes.core.v1.EnvVarArgs(
            name=name,
            value_from=kubernetes.core.v1.EnvVarSourceArgs(
                secret_key_ref=kubernetes.core.v1.SecretKeySelectorArgs(
                    name=oidc_secret_name, key=key
                )
            ),
        )

    sync_container = kubernetes.core.v1.ContainerArgs(
        name="sync",
        image=SYNC_IMAGE,
        command=[
            "python",
            f"{SCRIPT_MOUNT_PATH}/{SCRIPT_FILENAME}",
            "--in-cluster",
            f"--namespace={namespace}",
            f"--configmap={configmap_name}",
            f"--changed-file={CHANGED_FILE}",
        ],
        env=[
            _oidc_env("KEYCLOAK_ISSUER_URL", "OIDC_ISSUER_URL"),
            _oidc_env("KEYCLOAK_CLIENT_ID", "OIDC_CLIENT_ID"),
            _oidc_env("KEYCLOAK_CLIENT_SECRET", "OIDC_CLIENT_SECRET"),
        ],
        volume_mounts=[
            kubernetes.core.v1.VolumeMountArgs(
                name="script", mount_path=SCRIPT_MOUNT_PATH, read_only=True
            ),
            kubernetes.core.v1.VolumeMountArgs(name="work", mount_path=WORK_MOUNT_PATH),
            kubernetes.core.v1.VolumeMountArgs(
                name="service-account-token",
                mount_path=SERVICE_ACCOUNT_MOUNT_PATH,
                read_only=True,
            ),
        ],
        security_context=_RESTRICTED_SECURITY_CONTEXT,
        resources=_RESOURCES,
    )
    reload_container = kubernetes.core.v1.ContainerArgs(
        name="reload",
        image=RELOAD_IMAGE,
        command=[
            "sh",
            "-c",
            reload_command(
                group_provider, group_file_path, mysql_port, ssl_enabled=ssl_enabled
            ),
        ],
        env=[kubernetes.core.v1.EnvVarArgs(name="STARROCKS_HOST", value=fe_host)],
        env_from=[
            kubernetes.core.v1.EnvFromSourceArgs(
                secret_ref=kubernetes.core.v1.SecretEnvSourceArgs(
                    name=CREDENTIALS_SECRET_NAME
                )
            )
        ],
        volume_mounts=[
            kubernetes.core.v1.VolumeMountArgs(
                name="work", mount_path=WORK_MOUNT_PATH, read_only=True
            ),
        ],
        security_context=_RESTRICTED_SECURITY_CONTEXT,
        resources=_RESOURCES,
    )

    return kubernetes.batch.v1.CronJob(
        f"{GROUP_SYNC_NAME}-{suffix}",
        metadata=kubernetes.meta.v1.ObjectMetaArgs(
            name=GROUP_SYNC_NAME,
            namespace=namespace,
            labels=k8s_labels,
        ),
        spec=kubernetes.batch.v1.CronJobSpecArgs(
            schedule=GROUP_SYNC_SCHEDULE,
            concurrency_policy="Forbid",
            starting_deadline_seconds=300,
            successful_jobs_history_limit=1,
            failed_jobs_history_limit=5,
            job_template=kubernetes.batch.v1.JobTemplateSpecArgs(
                metadata=kubernetes.meta.v1.ObjectMetaArgs(labels=k8s_labels),
                spec=kubernetes.batch.v1.JobSpecArgs(
                    # The next scheduled run is the retry.
                    backoff_limit=0,
                    active_deadline_seconds=GROUP_SYNC_ACTIVE_DEADLINE_SECONDS,
                    template=kubernetes.core.v1.PodTemplateSpecArgs(
                        metadata=kubernetes.meta.v1.ObjectMetaArgs(
                            labels={
                                **k8s_labels,
                                "app.kubernetes.io/name": GROUP_SYNC_NAME,
                            },
                        ),
                        spec=kubernetes.core.v1.PodSpecArgs(
                            restart_policy="Never",
                            service_account_name=service_account.metadata.name,
                            # The token is projected into the sync container only;
                            # the reload container holds the SQL credential.
                            automount_service_account_token=False,
                            init_containers=[sync_container],
                            containers=[reload_container],
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
                                    empty_dir=kubernetes.core.v1.EmptyDirVolumeSourceArgs(
                                        size_limit="1Mi"
                                    ),
                                ),
                                kubernetes.core.v1.VolumeArgs(
                                    name="service-account-token",
                                    projected=kubernetes.core.v1.ProjectedVolumeSourceArgs(
                                        default_mode=0o440,
                                        sources=[
                                            kubernetes.core.v1.VolumeProjectionArgs(
                                                service_account_token=kubernetes.core.v1.ServiceAccountTokenProjectionArgs(
                                                    path="token",
                                                    expiration_seconds=SERVICE_ACCOUNT_TOKEN_SECONDS,
                                                )
                                            ),
                                            kubernetes.core.v1.VolumeProjectionArgs(
                                                config_map=kubernetes.core.v1.ConfigMapProjectionArgs(
                                                    name="kube-root-ca.crt",
                                                    items=[
                                                        kubernetes.core.v1.KeyToPathArgs(
                                                            key="ca.crt",
                                                            path="ca.crt",
                                                        )
                                                    ],
                                                )
                                            ),
                                        ],
                                    ),
                                ),
                            ],
                            security_context=kubernetes.core.v1.PodSecurityContextArgs(
                                fs_group=1000,
                                seccomp_profile=kubernetes.core.v1.SeccompProfileArgs(
                                    type="RuntimeDefault"
                                ),
                            ),
                        ),
                    ),
                ),
            ),
        ),
        opts=ResourceOptions(
            depends_on=[
                script_config_map,
                role_binding,
                credentials,
                *depends_on,
            ]
        ),
    )
