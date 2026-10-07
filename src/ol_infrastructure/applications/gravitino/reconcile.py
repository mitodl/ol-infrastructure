"""Deploy the Gravitino grant reconciler as a CronJob.

The reconciler is ``scripts/reconcile_gravitino_grants.py``; its docstring covers
what it converges and why. This module renders its desired state from
``lib/data_lake_access.py`` and deploys it on the same pattern as ``posture.py``:
a stdlib script in a ConfigMap on a stock Python image, with a non-zero exit as
the signal.

It is the only workload that mounts the management API's client certificate, and
it holds the ``ol-gravitino-admin`` Keycloak credential. Together those amount to
code execution in the Gravitino pod (docs/plans/gravitino-deployment-spec.md,
"The job system makes the admin credential a code-execution credential"), so the
pod gets no ServiceAccount token and nothing else is mounted.
"""

import json
from pathlib import Path
from typing import Any

import pulumi_kubernetes as kubernetes
from pulumi import Output, Resource, ResourceOptions

from ol_infrastructure.components.services.vault import (
    OLVaultK8SSecret,
    OLVaultK8SStaticSecretConfig,
)
from ol_infrastructure.lib.aws.eks_helper import cached_image_uri
from ol_infrastructure.lib.data_lake_access import (
    CATALOG_WIDE_WRITE_ROLES,
    GOVERNANCE_LAYER_ACCESS,
    GOVERNANCE_ROLES,
    RETIRED_ROLES,
    LakeAccess,
    layer_database,
)
from ol_infrastructure.lib.pulumi_helper import StackInfo

RECONCILE_IMAGE = cached_image_uri("python:3.12-slim")

RECONCILE_NAME = "gravitino-reconcile"
SCRIPT_MOUNT_PATH = "/opt/gravitino-reconcile"
SCRIPT_FILENAME = "reconcile_gravitino_grants.py"
DESIRED_STATE_MOUNT_PATH = "/etc/gravitino-reconcile"
DESIRED_STATE_FILENAME = "desired-state.json"
CLIENT_TLS_MOUNT_PATH = "/etc/gravitino-client-tls"
KEYCLOAK_SECRET_NAME = (
    "gravitino-reconcile-keycloak"  # pragma: allowlist secret  # noqa: S105
)

# Every 15 minutes, per the authorization spec (A7). Keep it in the fast bucket
# of the ScheduledJobStale rules in infrastructure/grafana_alerting/metric_rules/
# eks_general.py.
RECONCILE_SCHEDULE = "*/15 * * * *"
# One Keycloak request per realm user (A8) on top of the Gravitino calls.
RECONCILE_ACTIVE_DEADLINE_SECONDS = 600
RECONCILE_BACKOFF_LIMIT = 0

# The StarRocks client whose client roles are the governance roles.
GOVERNANCE_ROLE_CLIENT_ID = "ol-starrocks-client"
SCHEMA_OWNER_GROUP = "ol_data_engineer"

# USE_CATALOG and USE_SCHEMA are needed in addition to SELECT_TABLE, which alone
# is a 403. Privileges inherit downward, so SELECT_TABLE on a schema covers
# every table in it.
CATALOG_USE_PRIVILEGES = ("USE_CATALOG",)
SCHEMA_PRIVILEGES: dict[LakeAccess, tuple[str, ...]] = {
    LakeAccess.read: ("USE_SCHEMA", "SELECT_TABLE"),
    LakeAccess.write: ("USE_SCHEMA", "SELECT_TABLE", "MODIFY_TABLE", "CREATE_TABLE"),
}
CATALOG_WIDE_WRITE_PRIVILEGES = (
    "USE_CATALOG",
    "USE_SCHEMA",
    "CREATE_SCHEMA",
    "SELECT_TABLE",
    "MODIFY_TABLE",
    "CREATE_TABLE",
)


def _securable_object(
    object_type: str, full_name: str, privileges: tuple[str, ...]
) -> dict[str, Any]:
    return {
        "type": object_type,
        "fullName": full_name,
        "privileges": [
            {"name": privilege, "condition": "ALLOW"} for privilege in privileges
        ],
    }


def render_role_grants(
    env_suffix: str, catalog: str
) -> dict[str, list[dict[str, Any]]]:
    """Translate the governance layer mapping into Gravitino securable objects.

    :param env_suffix: The environment suffix, e.g. ``qa``.
    :param catalog: The Gravitino catalog the grants are made on.

    :returns: Every governance role mapped to the securable objects it should
        hold. A role with no access maps to an empty list, so the reconciler
        still creates it and revokes anything it holds.
    """
    grants: dict[str, list[dict[str, Any]]] = {}
    for role in GOVERNANCE_ROLES:
        if role in CATALOG_WIDE_WRITE_ROLES:
            grants[role] = [
                _securable_object("catalog", catalog, CATALOG_WIDE_WRITE_PRIVILEGES)
            ]
            continue
        layers = GOVERNANCE_LAYER_ACCESS[role]
        grants[role] = [
            _securable_object(
                "schema",
                f"{catalog}.{layer_database(env_suffix, layer)}",
                SCHEMA_PRIVILEGES[access],
            )
            for layer, access in sorted(layers.items())
        ]
        if layers:
            grants[role].insert(
                0, _securable_object("catalog", catalog, CATALOG_USE_PRIVILEGES)
            )
    return grants


def render_desired_state(
    env_suffix: str,
    metalake: str,
    catalog: str,
    catalog_properties: dict[str, str],
) -> dict[str, Any]:
    """Build the document the reconciler converges Gravitino on.

    :param env_suffix: The environment suffix, e.g. ``qa``.
    :param metalake: The metalake name.
    :param catalog: The catalog name.
    :param catalog_properties: Properties the catalog is created with.

    :returns: The desired state, ready for ``json.dumps``.
    """
    return {
        "metalake": metalake,
        "catalog": {
            "name": catalog,
            "type": "relational",
            "provider": "lakehouse-iceberg",
            "comment": f"MIT OL data lake ({env_suffix}), Glue-backed Iceberg tables",
            "properties": catalog_properties,
        },
        "roles": render_role_grants(env_suffix, catalog),
        "retired_roles": list(RETIRED_ROLES),
        "schema_owner_group": SCHEMA_OWNER_GROUP,
        # Per-developer dbt schemas share this prefix and get the same owner.
        "owned_schema_prefix": f"ol_warehouse_{env_suffix}_",
        "governance_role_client_id": GOVERNANCE_ROLE_CLIENT_ID,
    }


def create_grant_reconciler(  # noqa: PLR0913
    stack_info: StackInfo,
    namespace: str,
    k8s_labels: dict[str, str],
    management_uri: str,
    client_tls_secret_name: str,
    vault_auth_name: str,
    desired_state: Output[dict[str, Any]],
    depends_on: list[Resource],
) -> kubernetes.batch.v1.CronJob:
    """Provision the grant reconciler CronJob.

    :param management_uri: Base URL of the Gravitino management API.
    :param client_tls_secret_name: The ``kubernetes.io/tls`` Secret holding the
        reconciler's client certificate and the namespace CA.
    :param vault_auth_name: The VaultAuth the Keycloak credential is synced with.
    :param desired_state: Output of ``render_desired_state``.
    :param depends_on: Resources the first scheduled run needs in place.

    :returns: The CronJob.
    """
    script_body = (Path(__file__).parent / "scripts" / SCRIPT_FILENAME).read_text()

    script_config_map = kubernetes.core.v1.ConfigMap(
        f"gravitino-reconcile-script-{stack_info.env_suffix}",
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
    desired_state_config_map = kubernetes.core.v1.ConfigMap(
        f"gravitino-reconcile-desired-state-{stack_info.env_suffix}",
        metadata=kubernetes.meta.v1.ObjectMetaArgs(
            namespace=namespace,
            labels=k8s_labels,
        ),
        data={
            DESIRED_STATE_FILENAME: desired_state.apply(
                lambda state: json.dumps(state, indent=2, sort_keys=True)
            )
        },
        opts=ResourceOptions(
            replace_on_changes=["data"],
            delete_before_replace=False,
        ),
    )

    keycloak_secret = OLVaultK8SSecret(
        f"gravitino-reconcile-keycloak-{stack_info.env_suffix}",
        OLVaultK8SStaticSecretConfig(
            name=KEYCLOAK_SECRET_NAME,
            namespace=namespace,
            labels=k8s_labels,
            dest_secret_labels=k8s_labels,
            dest_secret_name=KEYCLOAK_SECRET_NAME,
            mount="secret-operations",
            mount_type="kv-v1",
            path="sso/gravitino-admin",
            templates={
                "KEYCLOAK_ISSUER_URL": '{{ get .Secrets "url" }}',
                "KEYCLOAK_CLIENT_ID": '{{ get .Secrets "client_id" }}',
                "KEYCLOAK_CLIENT_SECRET": '{{ get .Secrets "client_secret" }}',
            },
            refresh_after="1h",
            vaultauth=vault_auth_name,
        ),
        opts=ResourceOptions(delete_before_replace=True, depends_on=depends_on),
    )

    return kubernetes.batch.v1.CronJob(
        f"gravitino-reconcile-{stack_info.env_suffix}",
        metadata=kubernetes.meta.v1.ObjectMetaArgs(
            name=RECONCILE_NAME,
            namespace=namespace,
            labels=k8s_labels,
        ),
        spec=kubernetes.batch.v1.CronJobSpecArgs(
            schedule=RECONCILE_SCHEDULE,
            # Two runs converging at once would race each other's grants.
            concurrency_policy="Forbid",
            starting_deadline_seconds=300,
            successful_jobs_history_limit=1,
            failed_jobs_history_limit=5,
            job_template=kubernetes.batch.v1.JobTemplateSpecArgs(
                metadata=kubernetes.meta.v1.ObjectMetaArgs(labels=k8s_labels),
                spec=kubernetes.batch.v1.JobSpecArgs(
                    # The next scheduled run is the retry.
                    backoff_limit=RECONCILE_BACKOFF_LIMIT,
                    active_deadline_seconds=RECONCILE_ACTIVE_DEADLINE_SECONDS,
                    template=kubernetes.core.v1.PodTemplateSpecArgs(
                        metadata=kubernetes.meta.v1.ObjectMetaArgs(
                            labels={
                                **k8s_labels,
                                "app.kubernetes.io/name": RECONCILE_NAME,
                            },
                        ),
                        spec=kubernetes.core.v1.PodSpecArgs(
                            restart_policy="Never",
                            automount_service_account_token=False,
                            containers=[
                                kubernetes.core.v1.ContainerArgs(
                                    name="reconcile-gravitino-grants",
                                    image=RECONCILE_IMAGE,
                                    command=[
                                        "python",
                                        f"{SCRIPT_MOUNT_PATH}/{SCRIPT_FILENAME}",
                                    ],
                                    env=[
                                        kubernetes.core.v1.EnvVarArgs(
                                            name="GRAVITINO_MANAGEMENT_URI",
                                            value=management_uri,
                                        ),
                                        kubernetes.core.v1.EnvVarArgs(
                                            name="GRAVITINO_CA_FILE",
                                            value=f"{CLIENT_TLS_MOUNT_PATH}/ca.crt",
                                        ),
                                        kubernetes.core.v1.EnvVarArgs(
                                            name="GRAVITINO_CLIENT_CERT_FILE",
                                            value=f"{CLIENT_TLS_MOUNT_PATH}/tls.crt",
                                        ),
                                        kubernetes.core.v1.EnvVarArgs(
                                            name="GRAVITINO_CLIENT_KEY_FILE",
                                            value=f"{CLIENT_TLS_MOUNT_PATH}/tls.key",
                                        ),
                                        kubernetes.core.v1.EnvVarArgs(
                                            name="DESIRED_STATE_FILE",
                                            value=(
                                                f"{DESIRED_STATE_MOUNT_PATH}/"
                                                f"{DESIRED_STATE_FILENAME}"
                                            ),
                                        ),
                                    ],
                                    env_from=[
                                        kubernetes.core.v1.EnvFromSourceArgs(
                                            secret_ref=kubernetes.core.v1.SecretEnvSourceArgs(
                                                name=KEYCLOAK_SECRET_NAME
                                            )
                                        )
                                    ],
                                    volume_mounts=[
                                        kubernetes.core.v1.VolumeMountArgs(
                                            name="script",
                                            mount_path=SCRIPT_MOUNT_PATH,
                                            read_only=True,
                                        ),
                                        kubernetes.core.v1.VolumeMountArgs(
                                            name="desired-state",
                                            mount_path=DESIRED_STATE_MOUNT_PATH,
                                            read_only=True,
                                        ),
                                        kubernetes.core.v1.VolumeMountArgs(
                                            name="client-tls",
                                            mount_path=CLIENT_TLS_MOUNT_PATH,
                                            read_only=True,
                                        ),
                                    ],
                                    security_context=kubernetes.core.v1.SecurityContextArgs(
                                        run_as_non_root=True,
                                        run_as_user=1000,
                                        run_as_group=1000,
                                        allow_privilege_escalation=False,
                                        read_only_root_filesystem=True,
                                        capabilities=kubernetes.core.v1.CapabilitiesArgs(
                                            drop=["ALL"]
                                        ),
                                    ),
                                    resources=kubernetes.core.v1.ResourceRequirementsArgs(
                                        requests={"cpu": "25m", "memory": "64Mi"},
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
                                kubernetes.core.v1.VolumeArgs(
                                    name="desired-state",
                                    config_map=kubernetes.core.v1.ConfigMapVolumeSourceArgs(
                                        name=desired_state_config_map.metadata.name,
                                    ),
                                ),
                                kubernetes.core.v1.VolumeArgs(
                                    name="client-tls",
                                    secret=kubernetes.core.v1.SecretVolumeSourceArgs(
                                        secret_name=client_tls_secret_name,
                                        # Readable by the container's group
                                        # only; the key is not world-readable.
                                        default_mode=0o440,
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
        # A scheduled run before the server or its credential exists would fail
        # and alert.
        opts=ResourceOptions(
            depends_on=[
                script_config_map,
                desired_state_config_map,
                keycloak_secret,
                *depends_on,
            ]
        ),
    )
