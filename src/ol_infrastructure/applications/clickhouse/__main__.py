# ruff: noqa: E501
"""Deploy a multi-tenant ClickHouse cluster for LLMOps workloads on the data EKS cluster.

Architecture:
- Altinity ClickHouseInstallation CRD manages the cluster (installed by the
  substructure.aws.eks.data stack).
- ClickHouseKeeperInstallation CRD manages Keeper (3 replicas) for distributed coordination.
- Hot storage: local NVMe SSD via ``local-nvme`` when enabled by the data EKS
  stack; otherwise EBS gp3.
- Cold storage: S3 bucket with Intelligent-Tiering; ClickHouse moves data via
  table-level TTL MOVE expressions.
- Multi-tenancy: separate database + user + resource quota per LLMOps tool.
  Passwords are stored in Vault KV and synced to a K8s Secret as a users.xml
  file, which is volume-mounted into ClickHouse pods.

Requires the ``substructure.aws.eks.data.*`` stack to be deployed (which installs
the NVMe DaemonSet and local-path-provisioner) before deploying to QA/Production.
"""

import json
from pathlib import Path
from textwrap import dedent
from typing import Any

import pulumi_aws as aws
import pulumi_kubernetes as kubernetes
import pulumi_vault as vault
from pulumi import Config, Output, ResourceOptions, export

from bridge.lib.versions import (
    CLICKHOUSE_KEEPER_VERSION,
    CLICKHOUSE_OPERATOR_VERSION,  # noqa: F401 — for traceability
    CLICKHOUSE_SERVER_VERSION,
)
from ol_infrastructure.components.applications.eks import (
    OLEKSAuthBinding,
    OLEKSAuthBindingConfig,
)
from ol_infrastructure.components.aws.s3 import OLBucket, S3BucketConfig
from ol_infrastructure.components.services.vault import (
    OLVaultK8SSecret,
    OLVaultK8SStaticSecretConfig,
)
from ol_infrastructure.lib import pulumi_projects as projects
from ol_infrastructure.lib.aws.eks_helper import (
    check_cluster_namespace,
    setup_k8s_provider,
)
from ol_infrastructure.lib.aws.iam_helper import IAM_POLICY_VERSION
from ol_infrastructure.lib.ol_types import (
    AWSBase,
    BusinessUnit,
    K8sGlobalLabels,
    Services,
)
from ol_infrastructure.lib.pulumi_helper import (
    make_stack_reference,
    parse_stack,
    require_stack_output_value,
)
from ol_infrastructure.lib.vault import setup_vault_provider

setup_vault_provider()

clickhouse_config = Config("clickhouse")
vault_config = Config("vault")
stack_info = parse_stack()

vault_mount_stack = make_stack_reference(
    projects.VAULT_STATIC_MOUNTS, f"operations.{stack_info.name}"
)
cluster_stack = make_stack_reference(projects.EKS, f"data.{stack_info.name}")
stateful_workload_storage = require_stack_output_value(
    cluster_stack, "stateful_workload_storage"
)

clickhouse_vault_kv_path = vault_mount_stack.require_output("clickhouse_kv")["path"]

clickhouse_env = f"data-{stack_info.env_suffix}"
aws_config = AWSBase(
    tags={
        "OU": BusinessUnit.data,
        "Environment": clickhouse_env,
        "Application": "clickhouse",
        "Owner": "platform-engineering",
    }
)

k8s_global_labels = {
    "pulumi_managed": "true",
    "pulumi_stack": stack_info.k8s_name,
    "ol.mit.edu/stack": stack_info.k8s_name,
    "app.kubernetes.io/managed-by": "pulumi",
}

k8s_labels = K8sGlobalLabels(
    ou=BusinessUnit.data,
    service=Services.clickhouse,
    stack=stack_info,
)

setup_k8s_provider(kubeconfig=require_stack_output_value(cluster_stack, "kube_config"))
CLICKHOUSE_NAMESPACE = "clickhouse"
# Declared for every cluster in infrastructure/aws/eks; must exist before a PVC
# references it. It does NOT recover on its own once the class shows up: the CSI
# external-resizer emits `VolumeModifyFailed  VAC "ebs-gp3-iops-3000" does not
# exist.`, drops the PVC's key, and never re-enqueues it -- data-ci sat Pending
# for hours after the class appeared. Touching the PVC (an annotation, a relabel)
# does not re-enqueue it either; only restarting the resizer does:
# `kubectl -n kube-system rollout restart deployment ebs-csi-controller`.
# Apply the eks stack before this one. Rollback is
# `pulumi config set clickhouse:data_volume_attributes_class ebs-gp3-iops-75000`
# (production) or `ebs-gp3-iops-25000` (QA) -- those classes are declared there
# too -- followed by an apply. Each switch recreates the StatefulSets, so every
# replica restarts once.
EBS_GP3_IOPS_3000_VAC = "ebs-gp3-iops-3000"
# ClickHouse's conventional Prometheus port, used for both server and Keeper.
CLICKHOUSE_METRICS_PORT = 9363

cluster_stack.require_output("namespaces").apply(
    lambda ns: check_cluster_namespace(CLICKHOUSE_NAMESPACE, ns)
)

# Configuration values
hot_data_days = int(clickhouse_config.get("hot_data_days") or "7")
hot_storage_size = clickhouse_config.get("hot_storage_size") or "100Gi"
storage_class = stateful_workload_storage["storage_class"]
data_volume_attributes_class = (
    clickhouse_config.get("data_volume_attributes_class") or EBS_GP3_IOPS_3000_VAC
)
use_io_optimized_nodes = stateful_workload_storage["use_io_optimized_nodes"]
ch_replicas = int(clickhouse_config.get("replicas") or "1")
keeper_replicas = int(clickhouse_config.get("keeper_replicas") or "1")
ch_version = clickhouse_config.get("version") or CLICKHOUSE_SERVER_VERSION
ch_image = f"altinity/clickhouse-server:{ch_version}"
keeper_image = f"clickhouse/clickhouse-keeper:{CLICKHOUSE_KEEPER_VERSION}"

IO_OPTIMIZED_NODE_SELECTOR = {"ol.mit.edu/io_optimized": "true"}
IO_OPTIMIZED_TOLERATION = kubernetes.core.v1.TolerationArgs(
    key="ol.mit.edu/io-workload",
    operator="Equal",
    value="true",
    effect="NoSchedule",
)
IO_OPTIMIZED_NODE_AFFINITY = kubernetes.core.v1.AffinityArgs(
    node_affinity=kubernetes.core.v1.NodeAffinityArgs(
        required_during_scheduling_ignored_during_execution=kubernetes.core.v1.NodeSelectorArgs(
            node_selector_terms=[
                kubernetes.core.v1.NodeSelectorTermArgs(
                    match_expressions=[
                        kubernetes.core.v1.NodeSelectorRequirementArgs(
                            key="ol.mit.edu/io_optimized",
                            operator="In",
                            values=["true"],
                        )
                    ]
                )
            ]
        )
    )
)

# Profiles and quotas are expressed in the Altinity operator's native
# slash-path map form (configuration.profiles / configuration.quotas). The
# operator merges these into its own generated usersd configmap, so they live
# in the *users* configuration space (where UsersConfigAccessStorage looks them
# up) without us having to mount anything over /etc/clickhouse-server/users.d/.
_LLMOPS_PROFILES = {
    "llmops_profile/max_memory_usage": "4000000000",
    "llmops_profile/max_concurrent_queries_for_user": "10",
    "llmops_profile/readonly": "0",
}
_LLMOPS_QUOTAS = {
    "llmops_quota/interval/duration": "3600",
    "llmops_quota/interval/queries": "20000",
    "llmops_quota/interval/errors": "100",
    "llmops_quota/interval/result_rows": "1000000000",
    "llmops_quota/interval/read_rows": "10000000000",
    "llmops_quota/interval/execution_time": "3600",
}

# System-log hardening copied from the Opik chart's conf.d/system_tables.xml
# (chart 2.2.71), which exists because these tables filled a production disk
# (comet-ml/opik#6224). The bundled Opik ClickHouse is disabled here, so none of
# it was inherited. The operator's own config.d already gives query_log,
# part_log and trace_log a 30-day TTL; this file sorts after its 01-clickhouse-*
# files, so remove="1" on trace_log wins over the operator's replace="1".
# query_metric_log exists from 24.10 and latency_log from 25.3; older servers
# ignore the elements.
#
# One deviation: the chart removes asynchronous_metric_log, but its one-second
# BlockReadOps/BlockWriteOps samples are the evidence behind the data-volume
# IOPS sizing (infrastructure/aws/eks ebs-gp3-iops-3000). It keeps its default
# sort key and gets the same TTL instead.
#
# Changing a log table's engine does not alter the existing table. On the first
# flush after restart ClickHouse renames it to <table>_0 (which keeps its data
# and gets no TTL) and creates a new one. Removed tables also stay on disk. Both
# need a manual DROP after rollout; see the runbook comment further down.
SYSTEM_LOG_TTL_DAYS = 30
_REMOVED_SYSTEM_LOGS = (
    "opentelemetry_span_log",
    "processors_profile_log",
    "text_log",
    "trace_log",
    "blob_storage_log",
)
_TTL_SYSTEM_LOG_ORDER_BY = {
    "asynchronous_metric_log": "(metric, event_date, event_time)",
    "error_log": "(event_date, event_time)",
    "latency_log": "(event_date, event_time)",
    "metric_log": "(event_date, event_time)",
    "query_metric_log": "(event_date, event_time)",
}
SYSTEM_LOG_TABLES_XML = "\n".join(
    [
        "<clickhouse>",
        *(f'  <{table} remove="1"/>' for table in _REMOVED_SYSTEM_LOGS),
        *(
            dedent(f"""\
              <{table}>
                <database>system</database>
                <table>{table}</table>
                <engine>ENGINE = MergeTree PARTITION BY toYYYYMM(event_date) ORDER BY {order_by} TTL event_date + toIntervalDay({SYSTEM_LOG_TTL_DAYS}) SETTINGS index_granularity = 8192</engine>
              </{table}>""")
            for table, order_by in _TTL_SYSTEM_LOG_ORDER_BY.items()
        ),
        "</clickhouse>",
        "",
    ]
)


def _require_password(password_output: Output, username: str) -> Output:
    """Fail fast if a ClickHouse user password is left at the insecure default."""

    def _check(password: str) -> str:
        if password == "changeme":  # pragma: allowlist secret  # noqa: S105
            msg = (
                f"ClickHouse password for user '{username}' must be set via "
                f"'pulumi config set --secret clickhouse:{username}_password' "
                f"and cannot use the default 'changeme' value."  # pragma: allowlist secret
            )
            raise ValueError(msg)
        return password

    return password_output.apply(_check)


def _create_clickhouse_keeper_installation(  # noqa: PLR0913
    *,
    env_suffix: str,
    namespace: str,
    labels: dict[str, str],
    replicas: int,
    image: str,
    use_io_optimized: bool,
    ebs_storageclass_output: "Output[Any]",
    fallback_storage_class: str,
) -> "Output[kubernetes.apiextensions.CustomResource]":
    """Create a ClickHouseKeeperInstallation CRD resource managed by the Altinity operator.

    The operator automatically creates a service named ``keeper-clickhouse`` which is used
    by the ClickHouseInstallation for ZooKeeper-compatible coordination.
    """
    tolerations = (
        [
            {
                "key": "ol.mit.edu/io-workload",
                "operator": "Equal",
                "value": "true",
                "effect": "NoSchedule",
            }
        ]
        if use_io_optimized
        else []
    )
    node_selector = IO_OPTIMIZED_NODE_SELECTOR if use_io_optimized else {}
    affinity: dict[str, Any] = {}
    if replicas > 1:
        affinity["podAntiAffinity"] = {
            "requiredDuringSchedulingIgnoredDuringExecution": [
                {
                    "labelSelector": {
                        "matchExpressions": [
                            {
                                "key": "clickhouse-keeper.altinity.com/chk",
                                "operator": "In",
                                "values": ["clickhouse"],
                            }
                        ]
                    },
                    "topologyKey": "kubernetes.io/hostname",
                }
            ]
        }
    if use_io_optimized:
        affinity["nodeAffinity"] = {
            "requiredDuringSchedulingIgnoredDuringExecution": {
                "nodeSelectorTerms": [
                    {
                        "matchExpressions": [
                            {
                                "key": "ol.mit.edu/io_optimized",
                                "operator": "In",
                                "values": ["true"],
                            }
                        ]
                    }
                ]
            }
        }

    return ebs_storageclass_output.apply(
        lambda sc: kubernetes.apiextensions.CustomResource(
            f"clickhouse-keeper-installation-{env_suffix}",
            api_version="clickhouse-keeper.altinity.com/v1",
            kind="ClickHouseKeeperInstallation",
            metadata=kubernetes.meta.v1.ObjectMetaArgs(
                name="clickhouse",
                namespace=namespace,
                labels=labels,
            ),
            spec={
                "defaults": {
                    "templates": {
                        "podTemplate": "keeper-pod",
                        "dataVolumeClaimTemplate": "keeper-data",
                    }
                },
                "configuration": {
                    "clusters": [
                        {
                            "name": "cluster1",
                            "layout": {"replicasCount": replicas},
                        }
                    ],
                    "settings": {
                        "keeper_server/tcp_port": "2181",
                        "keeper_server/coordination_settings/raft_logs_level": "warning",
                        "keeper_server/coordination_settings/operation_timeout_ms": "10000",
                        "keeper_server/coordination_settings/session_timeout_ms": "30000",
                        "listen_host": "0.0.0.0",  # noqa: S104
                        # Keeper has no SQL interface for the operator's
                        # exporter to query, so its own endpoint is the only
                        # source of Keeper metrics (leader, synced followers,
                        # latency, outstanding requests).
                        "prometheus/endpoint": "/metrics",
                        "prometheus/port": str(CLICKHOUSE_METRICS_PORT),
                        "prometheus/metrics": "true",
                        "prometheus/events": "true",
                        "prometheus/asynchronous_metrics": "true",
                        "logger/level": "information",
                    },
                },
                "templates": {
                    "podTemplates": [
                        {
                            "name": "keeper-pod",
                            # Prevent Karpenter consolidation from disrupting single-replica Keeper.
                            **(
                                {
                                    "metadata": {
                                        "annotations": {
                                            "karpenter.sh/do-not-disrupt": "true"
                                        },
                                    },
                                }
                                if replicas == 1
                                else {}
                            ),
                            "spec": {
                                "securityContext": {"fsGroup": 101},
                                "tolerations": tolerations,
                                "nodeSelector": node_selector,
                                **({"affinity": affinity} if affinity else {}),
                                "containers": [
                                    {
                                        "name": "clickhouse-keeper",
                                        "image": image,
                                        "resources": {
                                            "requests": {
                                                "cpu": "100m",
                                                "memory": "256Mi",
                                            },
                                            "limits": {
                                                "memory": "512Mi",
                                            },
                                        },
                                    }
                                ],
                            },
                        }
                    ],
                    "volumeClaimTemplates": [
                        {
                            "name": "keeper-data",
                            "spec": {
                                "accessModes": ["ReadWriteOnce"],
                                "storageClassName": sc
                                if sc is not None
                                else fallback_storage_class,
                                "resources": {
                                    "requests": {
                                        "storage": "20Gi",
                                    }
                                },
                            },
                        }
                    ],
                },
            },
        )
    )


def _create_clickhouse_installation(  # noqa: PLR0913
    *,
    env_suffix: str,
    namespace: str,
    labels: dict[str, str],
    ch_replicas: int,
    ch_image: str,
    use_io_optimized: bool,
    storage_class: str,
    data_volume_attributes_class: str,
    hot_storage_size: str,
    cold_bucket_name: "Output[str]",
    backup_bucket_name: "Output[str]",
    users_secret_name: str,
    irsa_role_arn: "Output[str]",
    keeper_installation: "Output[kubernetes.apiextensions.CustomResource]",
    users_secret: Any,
    vault_k8s_resources: Any,
) -> "Output[kubernetes.apiextensions.CustomResource]":
    """Build the ClickHouseInstallation CRD and return it wrapped in an Output.

    Uses ``Output.all().apply()`` because the storage configuration XML must be
    resolved from the cold-storage and backup bucket names, which are
    ``Output[str]``.
    """
    ch_tolerations = (
        [
            {
                "key": "ol.mit.edu/io-workload",
                "operator": "Equal",
                "value": "true",
                "effect": "NoSchedule",
            }
        ]
        if use_io_optimized
        else []
    )
    ch_node_selector = IO_OPTIMIZED_NODE_SELECTOR if use_io_optimized else {}
    ch_affinity = (
        {
            "podAntiAffinity": {
                "requiredDuringSchedulingIgnoredDuringExecution": [
                    {
                        "labelSelector": {
                            "matchLabels": {"clickhouse.altinity.com/chi": "clickhouse"}
                        },
                        "topologyKey": "kubernetes.io/hostname",
                    }
                ]
            },
            **(
                {
                    "nodeAffinity": {
                        "requiredDuringSchedulingIgnoredDuringExecution": {
                            "nodeSelectorTerms": [
                                {
                                    "matchExpressions": [
                                        {
                                            "key": "ol.mit.edu/io_optimized",
                                            "operator": "In",
                                            "values": ["true"],
                                        }
                                    ]
                                }
                            ]
                        }
                    }
                }
                if use_io_optimized
                else {}
            ),
        }
        if ch_replicas > 1
        else (
            {
                "nodeAffinity": {
                    "requiredDuringSchedulingIgnoredDuringExecution": {
                        "nodeSelectorTerms": [
                            {
                                "matchExpressions": [
                                    {
                                        "key": "ol.mit.edu/io_optimized",
                                        "operator": "In",
                                        "values": ["true"],
                                    }
                                ]
                            }
                        ]
                    }
                }
            }
            if use_io_optimized
            else {}
        )
    )

    storage_config_xml = Output.all(cold_bucket_name, backup_bucket_name).apply(
        lambda buckets: dedent(f"""\
            <clickhouse>
              <storage_configuration>
                <disks>
                  <!-- The hot tier uses ClickHouse's built-in "default" disk,
                       which points at the server's main path
                       (/var/lib/clickhouse/, the NVMe or EBS PVC). A custom
                       local disk at that same path is rejected with
                       UNKNOWN_ELEMENT_IN_CONFIG ("cannot be equal to <path>"). -->
                  <cold_s3>
                    <type>s3</type>
                    <endpoint>https://{buckets[0]}.s3.amazonaws.com/data/</endpoint>
                    <use_environment_credentials>true</use_environment_credentials>
                    <metadata_path>/var/lib/clickhouse/disks/cold_s3/</metadata_path>
                  </cold_s3>
                  <!-- Destination for BACKUP ... TO Disk('backups', ...).
                       s3_plain rather than s3: an s3 disk keeps its object
                       metadata on the local PVC, so a backup written through
                       one could only be read back from the replica that wrote
                       it. s3_plain stores files under their real names, which
                       is what lets a different cluster or a rebuilt replica
                       read the backup. Not referenced by any storage policy,
                       so no table data lands here. -->
                  <backups>
                    <type>s3_plain</type>
                    <endpoint>https://{buckets[1]}.s3.amazonaws.com/backups/</endpoint>
                    <use_environment_credentials>true</use_environment_credentials>
                  </backups>
                </disks>
                <policies>
                  <tiered>
                    <volumes>
                      <hot>
                        <disk>default</disk>
                      </hot>
                      <cold>
                        <disk>cold_s3</disk>
                        <prefer_not_to_merge>true</prefer_not_to_merge>
                      </cold>
                    </volumes>
                  </tiered>
                </policies>
              </storage_configuration>
              <!-- Without this, Disk() is refused outright as a BACKUP target
                   (Code 318, "'backups.allowed_disk' configuration parameter is
                   not set"). -->
              <backups>
                <allowed_disk>backups</allowed_disk>
              </backups>
            </clickhouse>
        """)
    )

    # Operator-native user definitions. Password hashes are referenced from the
    # Vault-synced K8s secret via secretKeyRef (keys: <user>_sha256); the
    # operator reads them at reconcile and bakes them into its usersd configmap.
    # networks/ip is left open at the ClickHouse layer because the actual access
    # boundary is the NetworkPolicy defined below (only the LLMOps namespaces +
    # the clickhouse namespace can reach the data ports).
    def _secret_ref(key: str) -> dict[str, Any]:
        return {"valueFrom": {"secretKeyRef": {"name": users_secret_name, "key": key}}}

    ch_users = {
        "admin/password_sha256_hex": _secret_ref("admin_sha256"),
        "admin/access_management": "1",
        "admin/profile": "default",
        "admin/quota": "default",
        "admin/networks/ip": ["::/0", "0.0.0.0/0"],
        "opik/password_sha256_hex": _secret_ref("opik_sha256"),
        "opik/profile": "llmops_profile",
        "opik/quota": "llmops_quota",
        "opik/networks/ip": ["::/0", "0.0.0.0/0"],
        # Explicit RBAC grants (rendered as <grants><query>…</query></grants> in
        # the user's XML) instead of the legacy <allow_databases> whitelist.
        # ``allow_databases`` restricts the user's grants to *only* the listed
        # application databases, which denies opik the SELECT on ``system.parts``
        # it needs to compute per-workspace/table storage metrics (Code: 497
        # ACCESS_DENIED SELECT(...) ON system.parts). Enumerating grants lets us
        # add that one system-table read without opening the whole ``system``
        # database (preserving cross-tenant isolation vs. e.g. system.query_log).
        #
        # ``default`` is granted in addition to ``opik_db``: opik's Liquibase
        # migrations hard-code the DATABASECHANGELOG / DATABASECHANGELOGLOCK
        # bookkeeping tables (and the replicated ``...1`` shadow tables from
        # 000017_change_tables_to_replicated) into the ``default`` database,
        # while application tables live in ``opik_db``. ``default`` is otherwise
        # an unused scratch database in this cluster.
        #
        # ``CLUSTER ON *.*`` is required because newer opik migrations issue
        # distributed DDL with an explicit ``ON CLUSTER '{cluster}'`` clause
        # (e.g. 000098_create_cipx_spend_blocks). Altinity 24.8 defaults
        # ``on_cluster_queries_require_cluster_grant`` to true, and CLUSTER is a
        # global-only privilege not implied by the database-scoped ``GRANT ALL``
        # above (without it: Code 497 ACCESS_DENIED, grant CLUSTER ON *.*). This
        # only permits *issuing* ON CLUSTER queries; the underlying object access
        # stays constrained by the opik_db / default grants, so cross-tenant
        # isolation is preserved.
        "opik/grants/query": [
            "GRANT ALL ON opik_db.*",
            "GRANT ALL ON default.*",
            "GRANT SELECT ON system.parts",
            "GRANT CLUSTER ON *.*",
        ],
    }

    return Output.all(
        storage_config=storage_config_xml,
        irsa_role_arn=irsa_role_arn,
    ).apply(
        lambda kwargs: kubernetes.apiextensions.CustomResource(
            f"clickhouse-installation-{env_suffix}",
            api_version="clickhouse.altinity.com/v1",
            kind="ClickHouseInstallation",
            metadata=kubernetes.meta.v1.ObjectMetaArgs(
                name="clickhouse",
                namespace=namespace,
                labels=labels,
            ),
            spec={
                "defaults": {
                    "templates": {
                        "podTemplate": "clickhouse-pod-template",
                        "dataVolumeClaimTemplate": "clickhouse-data",
                    }
                },
                "configuration": {
                    "zookeeper": {
                        "nodes": [
                            {
                                "host": f"keeper-clickhouse.{namespace}.svc.cluster.local",
                                "port": 2181,
                            }
                        ]
                    },
                    "clusters": [
                        {
                            "name": "default",
                            "layout": {
                                "shardsCount": 1,
                                "replicasCount": ch_replicas,
                            },
                        }
                    ],
                    "files": {
                        "config.d/storage.xml": kwargs["storage_config"],
                        "config.d/system_log_tables.xml": SYSTEM_LOG_TABLES_XML,
                    },
                    "settings": {
                        "default_storage_policy": "tiered",
                        # Server default is 0.9 of the cgroup memory limit;
                        # 0.85 matches the Opik chart and leaves more of the
                        # limit for memory the server does not track before
                        # the kernel OOM-kills the container.
                        "max_server_memory_usage_to_ram_ratio": "0.85",
                        # Built-in Prometheus endpoint. Without a <prometheus>
                        # section ClickHouse serves no metrics at all; the old
                        # ServiceMonitor on 8123/metrics scraped up=0 on every
                        # replica. The operator's own exporter (kube-system,
                        # clickhouse-operator-metrics:8888) covers replication
                        # and table state; this adds the server's full
                        # metrics/events/asynchronous_metrics/errors.
                        "prometheus/endpoint": "/metrics",
                        "prometheus/port": str(CLICKHOUSE_METRICS_PORT),
                        "prometheus/metrics": "true",
                        "prometheus/events": "true",
                        "prometheus/asynchronous_metrics": "true",
                        "prometheus/errors": "true",
                        # Debug by default: ~1.7M lines/day per cluster to
                        # Loki (12.08M over the 7 days to 2026-09-10 in
                        # data-production). The operator applies logger
                        # changes without a restart.
                        "logger/level": "information",
                    },
                    # Users, profiles, and quotas are managed by the operator
                    # (merged into its generated usersd configmap) rather than
                    # mounted as a secret over /etc/clickhouse-server/users.d/,
                    # which would shadow the operator's own usersd files (the
                    # clickhouse_operator management user and base profiles).
                    # Password hashes are pulled from the Vault-synced secret
                    # via secretKeyRef so plaintext never enters the CHI spec.
                    "profiles": _LLMOPS_PROFILES,
                    "quotas": _LLMOPS_QUOTAS,
                    "users": ch_users,
                },
                "templates": {
                    "podTemplates": [
                        {
                            "name": "clickhouse-pod-template",
                            # Prevent Karpenter consolidation from disrupting single-replica ClickHouse.
                            **(
                                {
                                    "metadata": {
                                        "annotations": {
                                            "karpenter.sh/do-not-disrupt": "true"
                                        },
                                    },
                                }
                                if ch_replicas == 1
                                else {}
                            ),
                            "spec": {
                                "serviceAccountName": "clickhouse",
                                "tolerations": ch_tolerations,
                                "nodeSelector": ch_node_selector,
                                "affinity": ch_affinity,
                                "containers": [
                                    {
                                        "name": "clickhouse",
                                        "image": ch_image,
                                        "resources": {
                                            "requests": {
                                                "cpu": "500m",
                                                "memory": "4Gi",
                                            },
                                            "limits": {
                                                "memory": "8Gi",
                                            },
                                        },
                                        "env": [
                                            {
                                                "name": "AWS_ROLE_ARN",
                                                "value": kwargs["irsa_role_arn"],
                                            },
                                            {
                                                "name": "AWS_WEB_IDENTITY_TOKEN_FILE",
                                                "value": "/var/run/secrets/eks.amazonaws.com/serviceaccount/token",
                                            },
                                        ],
                                    }
                                ],
                            },
                        }
                    ],
                    "volumeClaimTemplates": [
                        {
                            "name": "clickhouse-data",
                            "spec": {
                                "accessModes": ["ReadWriteOnce"],
                                "storageClassName": storage_class,
                                # ebs-gp3-sc derives IOPS from size (iopsPerGB), which
                                # gave the 1500Gi production volumes 75,000 IOPS each
                                # against a one-second peak under 2,000 ops/s. The class
                                # pins them to gp3's free 3,000. The operator applies a
                                # template VAC to existing PVCs on reconcile (0.26.0
                                # storage-reconciler.go reconcileVolumeAttributeClass),
                                # and the CSI driver modifies the EBS volume online.
                                **(
                                    {
                                        "volumeAttributesClassName": data_volume_attributes_class
                                    }
                                    if storage_class == "ebs-gp3-sc"
                                    else {}
                                ),
                                "resources": {
                                    "requests": {
                                        "storage": hot_storage_size,
                                    }
                                },
                            },
                        }
                    ],
                },
            },
            opts=ResourceOptions(
                depends_on=[
                    keeper_installation,
                    users_secret,
                    vault_k8s_resources,
                ],
            ),
        )
    )


# Per-tool passwords from stack config (must be set with `pulumi config set --secret`)
admin_password = _require_password(
    clickhouse_config.get_secret("admin_password")
    or Output.secret("changeme"),  # pragma: allowlist secret
    "admin",
)
opik_password = _require_password(
    clickhouse_config.get_secret("opik_password")
    or Output.secret("changeme"),  # pragma: allowlist secret
    "opik",
)

############################################################
# S3 Cold Storage Bucket
############################################################
cold_bucket_name = f"ol-data-clickhouse-cold-{stack_info.env_suffix}"

cold_bucket_config = S3BucketConfig(
    bucket_name=cold_bucket_name,
    tags=aws_config.tags,
    versioning_enabled=False,
    server_side_encryption_enabled=True,
    sse_algorithm="AES256",
    intelligent_tiering_enabled=True,
    intelligent_tiering_days=1,  # Move to Intelligent-Tiering immediately
    intelligent_tiering_archive_access_days=None,
    intelligent_tiering_deep_archive_access_days=None,
    # ClickHouse issues synchronous S3 reads against this disk for any query
    # that touches a cold part, so archive tiers (which require an explicit
    # restore before the object is readable) are not safe here even though
    # the bucket itself is never user-facing.
)

cold_bucket = OLBucket(
    f"clickhouse-cold-storage-{stack_info.env_suffix}",
    cold_bucket_config,
)

export("cold_bucket_name", cold_bucket.bucket_v2.bucket)
export("cold_bucket_arn", cold_bucket.bucket_v2.arn)

############################################################
# S3 Backup Bucket
#
# Destination of the daily SQL BACKUP (see the CronJob below). Kept apart from
# the cold-tier bucket: once tiering is activated that bucket holds live parts,
# and a backup sharing a bucket with the data it protects shares its failure
# modes (a bad lifecycle rule, a mistaken delete) too.
#
# ClickHouse's own IRSA role writes here, so it can also delete. Versioning is
# what makes a backup deleted or overwritten through that role recoverable for
# a week afterwards. Each run is a full backup (no base_backup chain), so
# expiring by object age never strands a backup that a newer one depends on.
############################################################
backup_retention_days = int(clickhouse_config.get("backup_retention_days") or "14")
backup_bucket_name = f"ol-data-clickhouse-backup-{stack_info.env_suffix}"

backup_bucket = OLBucket(
    f"clickhouse-backup-{stack_info.env_suffix}",
    S3BucketConfig(
        bucket_name=backup_bucket_name,
        tags=aws_config.tags,
        versioning_enabled=True,
        server_side_encryption_enabled=True,
        sse_algorithm="AES256",
        # Objects live two weeks; Intelligent-Tiering's 30-day monitoring
        # window never pays back on them.
        intelligent_tiering_enabled=False,
        noncurrent_version_expiration_days=7,
        lifecycle_rules=[
            aws.s3.BucketLifecycleConfigurationRuleArgs(
                id="expire-backups",
                status="Enabled",
                filter=aws.s3.BucketLifecycleConfigurationRuleFilterArgs(
                    prefix="backups/"
                ),
                expiration=aws.s3.BucketLifecycleConfigurationRuleExpirationArgs(
                    days=backup_retention_days
                ),
            )
        ],
    ),
)

export("backup_bucket_name", backup_bucket.bucket_v2.bucket)
export("backup_bucket_arn", backup_bucket.bucket_v2.arn)

############################################################
# IRSA + Vault Auth Binding for ClickHouse
############################################################

# Build the IAM policy JSON as an Output to resolve bucket ARN values
clickhouse_s3_policy_json: Output[str] = Output.all(
    bucket_arn=cold_bucket.bucket_v2.arn,
    backup_bucket_arn=backup_bucket.bucket_v2.arn,
).apply(
    lambda args: json.dumps(
        {
            "Version": IAM_POLICY_VERSION,
            "Statement": [
                {
                    "Effect": "Allow",
                    "Action": [
                        "s3:GetObject",
                        "s3:PutObject",
                        "s3:DeleteObject",
                        "s3:ListBucket",
                        "s3:GetBucketLocation",
                        "s3:AbortMultipartUpload",
                        "s3:ListMultipartUploadParts",
                    ],
                    "Resource": [
                        args["bucket_arn"],
                        f"{args['bucket_arn']}/*",
                        args["backup_bucket_arn"],
                        f"{args['backup_bucket_arn']}/*",
                    ],
                },
            ],
        }
    )
)

clickhouse_app = OLEKSAuthBinding(
    OLEKSAuthBindingConfig(
        application_name="clickhouse",
        namespace=CLICKHOUSE_NAMESPACE,
        stack_info=stack_info,
        aws_config=aws_config,
        iam_policy_document=None,
        vault_policy_path=Path(__file__).parent.joinpath("clickhouse_policy.hcl"),
        cluster_name=cluster_stack.require_output("cluster_name"),
        cluster_identities=cluster_stack.require_output("cluster_identities"),
        vault_auth_endpoint=cluster_stack.require_output("vault_auth_endpoint"),
        irsa_service_account_name="clickhouse",
        vault_sync_service_account_names="clickhouse-vault",
        # The CHI pod template references serviceAccountName "clickhouse"; the
        # component must create that ServiceAccount (with the IRSA role-arn
        # annotation) since nothing else in the cluster does.
        create_irsa_service_account=True,
        k8s_labels=k8s_labels,
    )
)

clickhouse_s3_iam_policy = aws.iam.Policy(
    f"clickhouse-s3-policy-{stack_info.env_suffix}",
    name=f"clickhouse-s3-policy-{stack_info.env_suffix}",
    path=f"/ol-data/clickhouse-s3-policy-{stack_info.env_suffix}/",
    policy=clickhouse_s3_policy_json,
    description="Policy for granting ClickHouse access to cold storage S3 bucket",
)

aws.iam.RolePolicyAttachment(
    f"clickhouse-s3-policy-attach-{stack_info.env_suffix}",
    policy_arn=clickhouse_s3_iam_policy.arn,
    role=clickhouse_app.irsa_role.name,
)

############################################################
# Vault KV Secrets — store credentials in Vault
############################################################
vault.kv.SecretV2(
    f"clickhouse-vault-kv-secrets-{stack_info.env_suffix}",
    mount=clickhouse_vault_kv_path,
    name="credentials",
    data_json=Output.all(
        admin=admin_password,
        opik=opik_password,
    ).apply(json.dumps),
)

############################################################
# VSO K8s Secret — per-user SHA256 password hashes
#
# The CHI references these via secretKeyRef (configuration.users), so the
# operator merges the hashes into its own usersd configmap. We intentionally
# do NOT render a users.xml here: mounting that file over
# /etc/clickhouse-server/users.d/ would shadow the operator's own usersd files
# (the clickhouse_operator management user and base profiles), which is what
# caused the THERE_IS_NO_PROFILE crash loop.
#
# Each template emits a single secret key holding the SHA256 hex of the
# plaintext password (Sprig's sha256sum), so plaintext never leaves Vault.
############################################################
USERS_HASH_TEMPLATES = {
    "admin_sha256": '{{ get .Secrets "admin" | sha256sum }}',
    "opik_sha256": '{{ get .Secrets "opik" | sha256sum }}',
}

users_secret_name = "clickhouse-users"  # pragma: allowlist secret  # noqa: S105
users_secret = OLVaultK8SSecret(
    f"clickhouse-users-secret-{stack_info.env_suffix}",
    resource_config=OLVaultK8SStaticSecretConfig(
        name="clickhouse-users-config",
        namespace=CLICKHOUSE_NAMESPACE,
        labels=k8s_global_labels,
        dest_secret_labels=k8s_global_labels,
        dest_secret_name=users_secret_name,
        dest_secret_type="Opaque",  # pragma: allowlist secret  # noqa: S106
        mount=clickhouse_vault_kv_path,
        mount_type="kv-v2",
        path="credentials",
        templates=USERS_HASH_TEMPLATES,
        refresh_after="1h",
        vaultauth=clickhouse_app.vault_k8s_resources.auth_name,
    ),
    opts=ResourceOptions(
        delete_before_replace=True,
        depends_on=clickhouse_app.vault_k8s_resources,
    ),
)

############################################################
# ClickHouse Keeper — ClickHouseKeeperInstallation CRD
#
# Keeper provides distributed coordination (ZooKeeper-compatible protocol)
# required for ClickHouse replication. Managed by the Altinity operator,
# which auto-creates the service "keeper-clickhouse". Uses EBS gp3 for
# its own storage since it stores only coordination logs (not bulk data).
############################################################
keeper_installation = _create_clickhouse_keeper_installation(
    env_suffix=stack_info.env_suffix,
    namespace=CLICKHOUSE_NAMESPACE,
    labels=k8s_global_labels,
    replicas=keeper_replicas,
    image=keeper_image,
    use_io_optimized=use_io_optimized_nodes,
    ebs_storageclass_output=cluster_stack.get_output("ebs_storageclass"),
    fallback_storage_class=storage_class,
)

############################################################
# ClickHouseInstallation CRD
#
# The Altinity operator watches for ClickHouseInstallation resources and
# creates StatefulSets, Services, and ConfigMaps accordingly.
#
# Storage policy:
#   default  (built-in disk = NVMe or EBS PVC at /var/lib/clickhouse/) — fast writes
#   cold_s3  (S3 bucket, IRSA credentials via pod service account)
#
# Users, profiles, and quotas are defined in the CHI configuration above and
# managed by the operator; password hashes are pulled from the Vault-synced
# K8s secret via secretKeyRef (no secret volume is mounted over users.d/).
############################################################
clickhouse_installation = _create_clickhouse_installation(
    env_suffix=stack_info.env_suffix,
    namespace=CLICKHOUSE_NAMESPACE,
    labels=k8s_global_labels,
    ch_replicas=ch_replicas,
    ch_image=ch_image,
    use_io_optimized=use_io_optimized_nodes,
    storage_class=storage_class,
    data_volume_attributes_class=data_volume_attributes_class,
    hot_storage_size=hot_storage_size,
    cold_bucket_name=cold_bucket.bucket_v2.bucket,
    backup_bucket_name=backup_bucket.bucket_v2.bucket,
    users_secret_name=users_secret_name,
    irsa_role_arn=clickhouse_app.irsa_role.arn,
    keeper_installation=keeper_installation,
    users_secret=users_secret,
    vault_k8s_resources=clickhouse_app.vault_k8s_resources,
)

############################################################
# Create per-tool databases
# NOTE: ClickHouse SQL resources cannot be created declaratively via Pulumi
# since there is no ClickHouse Pulumi provider. The databases listed here must
# be created manually (or via an init job) after the cluster is up:
#
#   kubectl exec -it chi-clickhouse-default-0-0 -n clickhouse -- \
#     clickhouse-client --user admin --password <admin_password> \
#     --query "CREATE DATABASE IF NOT EXISTS opik_db"
#
# Required databases: opik_db
#
# After a change to SYSTEM_LOG_TABLES_XML rolls out, list leftover log tables
# on every replica (system tables are local, not replicated):
#
#   SELECT name, formatReadableSize(total_bytes) FROM system.tables
#   WHERE database = 'system' AND match(name, '_log(_[0-9]+)?$')
#
# and DROP TABLE system.<name> SYNC for each removed log and each renamed
# <table>_N. The server never writes to either again.
############################################################

############################################################
# Daily SQL backup
#
# Two recovery layers exist, and this is the second:
#   1. AWS Backup (infrastructure/aws/eks/aws_backup.py) snapshots every CH and
#      Keeper EBS volume daily at 05:00 UTC, kept 14 days. Crash-consistent and
#      per volume, uncoordinated across replicas: good for "a PVC died", not
#      for restoring into another cluster or across a server version.
#   2. This job: BACKUP of every non-system database to the backup bucket via
#      the s3_plain ``backups`` disk. Application-consistent per table and
#      readable by any ClickHouse at the same or a newer version.
#
# Runs against replica 0 by name, not the load-balanced service. With one
# shard every replica holds the same replicated data, but Opik's Liquibase
# ledger (default.DATABASECHANGELOG*) has rows only on replica 0, where
# migrations are pinned (CLICKHOUSE_MIGRATIONS_HOST in applications/opik).
# Replicas 1 and 2 carry empty tables of the same name. A restore without the
# ledger would replay every migration against existing tables.
#
# ASYNC plus polling rather than a synchronous BACKUP: the statement's outcome
# lives in system.backups on the server, so the job exits non-zero on
# BACKUP_FAILED instead of depending on how long the client connection survives
# a multi-minute query. system.backups is in-memory per server, which is why
# every query here targets the same host.
############################################################
BACKUP_HOST = f"chi-clickhouse-default-0-0.{CLICKHOUSE_NAMESPACE}.svc.cluster.local"
BACKUP_SCRIPT = """\
set -eu
ch() {
    clickhouse-client --host "${CLICKHOUSE_HOST}" --user admin \\
        --password "${CLICKHOUSE_PASSWORD}" "$@"
}
name="$(date -u +%Y%m%dT%H%M%SZ)"
id="$(ch --query "BACKUP ALL EXCEPT DATABASES system, INFORMATION_SCHEMA, information_schema TO Disk('backups', '${name}') ASYNC" | cut -f1)"
echo "backup ${name} started as ${id}"
while :; do
    status="$(ch --param_id="${id}" --query "SELECT status FROM system.backups WHERE id = {id:String}")"
    case "${status}" in
        BACKUP_CREATED) break ;;
        CREATING_BACKUP) sleep 30 ;;
        *)
            ch --param_id="${id}" --query "SELECT status, error FROM system.backups WHERE id = {id:String} FORMAT Vertical" >&2
            exit 1
            ;;
    esac
done
ch --param_id="${id}" --query "SELECT name, num_files, formatReadableSize(total_size) AS size, end_time - start_time AS seconds FROM system.backups WHERE id = {id:String} FORMAT Vertical"
"""

backup_credentials_secret_name = (
    "clickhouse-backup-credentials"  # pragma: allowlist secret  # noqa: S105
)
backup_credentials_secret = OLVaultK8SSecret(
    f"clickhouse-backup-credentials-{stack_info.env_suffix}",
    resource_config=OLVaultK8SStaticSecretConfig(
        name="clickhouse-backup-credentials",
        namespace=CLICKHOUSE_NAMESPACE,
        labels=k8s_global_labels,
        dest_secret_labels=k8s_global_labels,
        dest_secret_name=backup_credentials_secret_name,
        dest_secret_type="Opaque",  # pragma: allowlist secret  # noqa: S106
        mount=clickhouse_vault_kv_path,
        mount_type="kv-v2",
        path="credentials",
        templates={"CLICKHOUSE_PASSWORD": '{{- get .Secrets "admin" -}}'},
        refresh_after="1h",
        vaultauth=clickhouse_app.vault_k8s_resources.auth_name,
    ),
    opts=ResourceOptions(
        delete_before_replace=True,
        depends_on=clickhouse_app.vault_k8s_resources,
    ),
)

clickhouse_backup_cron_job = kubernetes.batch.v1.CronJob(
    f"clickhouse-backup-{stack_info.env_suffix}",
    metadata=kubernetes.meta.v1.ObjectMetaArgs(
        name="clickhouse-backup",
        namespace=CLICKHOUSE_NAMESPACE,
        labels=k8s_global_labels,
    ),
    spec=kubernetes.batch.v1.CronJobSpecArgs(
        # 90 minutes ahead of the 05:00 UTC AWS Backup snapshot, so a bad night
        # for one layer is not also the same moment for the other.
        schedule=clickhouse_config.get("backup_schedule") or "30 3 * * *",
        concurrency_policy="Forbid",
        starting_deadline_seconds=3600,
        successful_jobs_history_limit=3,
        failed_jobs_history_limit=3,
        job_template=kubernetes.batch.v1.JobTemplateSpecArgs(
            # On the Job as well as its pods, so `kubectl get jobs -l
            # app.kubernetes.io/name=clickhouse-backup` (the runbook's check)
            # finds the runs.
            metadata=kubernetes.meta.v1.ObjectMetaArgs(
                labels={
                    **k8s_global_labels,
                    "app.kubernetes.io/name": "clickhouse-backup",
                },
            ),
            spec=kubernetes.batch.v1.JobSpecArgs(
                # No retry: the server keeps running an ASYNC backup after the
                # client pod dies, so a retry could start a second one beside
                # it. The next night is the retry, and a failed Job alerts now.
                backoff_limit=0,
                active_deadline_seconds=7200,
                template=kubernetes.core.v1.PodTemplateSpecArgs(
                    metadata=kubernetes.meta.v1.ObjectMetaArgs(
                        labels={
                            **k8s_global_labels,
                            "app.kubernetes.io/name": "clickhouse-backup",
                        },
                    ),
                    spec=kubernetes.core.v1.PodSpecArgs(
                        restart_policy="Never",
                        containers=[
                            kubernetes.core.v1.ContainerArgs(
                                name="backup",
                                # Same image as the server, for a client that
                                # speaks exactly the server's protocol version.
                                image=ch_image,
                                command=["/bin/sh", "-c", BACKUP_SCRIPT],
                                env=[
                                    kubernetes.core.v1.EnvVarArgs(
                                        name="CLICKHOUSE_HOST", value=BACKUP_HOST
                                    ),
                                ],
                                env_from=[
                                    kubernetes.core.v1.EnvFromSourceArgs(
                                        secret_ref=kubernetes.core.v1.SecretEnvSourceArgs(
                                            name=backup_credentials_secret_name
                                        )
                                    )
                                ],
                                resources=kubernetes.core.v1.ResourceRequirementsArgs(
                                    requests={"cpu": "50m", "memory": "64Mi"},
                                    limits={"cpu": "200m", "memory": "256Mi"},
                                ),
                            )
                        ],
                    ),
                ),
            ),
        ),
    ),
    opts=ResourceOptions(
        depends_on=[clickhouse_installation, backup_credentials_secret],
    ),
)

############################################################
# Networking — ClusterIP Service + NetworkPolicy
#
# The Altinity operator creates per-replica services automatically.
# We also create:
#   • A stable ClusterIP service (``clickhouse``) that load-balances across
#     all healthy ClickHouse replicas via the HTTP (8123) and Native (9000)
#     ports for intra-cluster LLMOps clients.
#   • A NetworkPolicy that restricts external access: only pods within the
#     ``clickhouse`` namespace and the LLMOps namespaces (opik) may reach
#     ClickHouse on the data ports; inter-replica and Keeper traffic is
#     allowed namespace-wide.
############################################################
clickhouse_client_service = kubernetes.core.v1.Service(
    f"clickhouse-client-service-{stack_info.env_suffix}",
    metadata=kubernetes.meta.v1.ObjectMetaArgs(
        name="clickhouse",
        namespace=CLICKHOUSE_NAMESPACE,
        labels={**k8s_global_labels, "app": "clickhouse"},
    ),
    spec=kubernetes.core.v1.ServiceSpecArgs(
        type="ClusterIP",
        selector={"clickhouse.altinity.com/chi": "clickhouse"},
        ports=[
            kubernetes.core.v1.ServicePortArgs(
                name="http",
                port=8123,
                target_port=8123,
            ),
            kubernetes.core.v1.ServicePortArgs(
                name="native",
                port=9000,
                target_port=9000,
            ),
            kubernetes.core.v1.ServicePortArgs(
                name="metrics",
                port=CLICKHOUSE_METRICS_PORT,
                target_port=CLICKHOUSE_METRICS_PORT,
            ),
        ],
    ),
)

# The operator's keeper-clickhouse Service exposes only 2181 and the raft port,
# so Keeper's metrics port needs a Service of its own for a ServiceMonitor to
# select. Headless because Prometheus scrapes each pod's endpoint, never the
# Service address.
keeper_metrics_service = kubernetes.core.v1.Service(
    f"clickhouse-keeper-metrics-service-{stack_info.env_suffix}",
    metadata=kubernetes.meta.v1.ObjectMetaArgs(
        name="clickhouse-keeper-metrics",
        namespace=CLICKHOUSE_NAMESPACE,
        labels={**k8s_global_labels, "app": "clickhouse-keeper"},
    ),
    spec=kubernetes.core.v1.ServiceSpecArgs(
        cluster_ip="None",
        selector={"clickhouse-keeper.altinity.com/chk": "clickhouse"},
        ports=[
            kubernetes.core.v1.ServicePortArgs(
                name="metrics",
                port=CLICKHOUSE_METRICS_PORT,
                target_port=CLICKHOUSE_METRICS_PORT,
            ),
        ],
    ),
)

# NOTE: These namespaces must exist on the data EKS cluster and be included in
# the `eks:namespaces` list in the `infrastructure.aws.eks.data.*` stack
# configurations. If they are missing, this NetworkPolicy will not allow
# traffic from LLMOps workloads in those namespaces as intended.
LLMOPS_NAMESPACES = ["opik"]

clickhouse_network_policy = kubernetes.networking.v1.NetworkPolicy(
    f"clickhouse-network-policy-{stack_info.env_suffix}",
    metadata=kubernetes.meta.v1.ObjectMetaArgs(
        name="clickhouse-allow-llmops",
        namespace=CLICKHOUSE_NAMESPACE,
        labels=k8s_global_labels,
    ),
    spec=kubernetes.networking.v1.NetworkPolicySpecArgs(
        pod_selector=kubernetes.meta.v1.LabelSelectorArgs(
            match_labels={"clickhouse.altinity.com/chi": "clickhouse"},
        ),
        policy_types=["Ingress"],
        ingress=[
            # Allow same-namespace traffic (Keeper, operator, admin)
            kubernetes.networking.v1.NetworkPolicyIngressRuleArgs(
                from_=[
                    kubernetes.networking.v1.NetworkPolicyPeerArgs(
                        namespace_selector=kubernetes.meta.v1.LabelSelectorArgs(
                            match_labels={
                                "kubernetes.io/metadata.name": CLICKHOUSE_NAMESPACE
                            },
                        )
                    )
                ],
            ),
            # Allow HTTP + Native from LLMOps namespaces
            kubernetes.networking.v1.NetworkPolicyIngressRuleArgs(
                from_=[
                    kubernetes.networking.v1.NetworkPolicyPeerArgs(
                        namespace_selector=kubernetes.meta.v1.LabelSelectorArgs(
                            match_expressions=[
                                kubernetes.meta.v1.LabelSelectorRequirementArgs(
                                    key="kubernetes.io/metadata.name",
                                    operator="In",
                                    values=LLMOPS_NAMESPACES,
                                )
                            ]
                        )
                    )
                ],
                ports=[
                    kubernetes.networking.v1.NetworkPolicyPortArgs(port=8123),
                    kubernetes.networking.v1.NetworkPolicyPortArgs(port=9000),
                ],
            ),
            # Allow Prometheus scraping from Alloy, which runs in the `grafana`
            # namespace (this used to name `monitoring`, where nothing runs).
            #
            # NetworkPolicy is not enforced on the data clusters today
            # (amazon-vpc-cni enable-network-policy-controller: "false"), so
            # none of these rules restrict anything yet. They are kept
            # accurate so that turning enforcement on does not cut off
            # scraping or clients.
            kubernetes.networking.v1.NetworkPolicyIngressRuleArgs(
                from_=[
                    kubernetes.networking.v1.NetworkPolicyPeerArgs(
                        namespace_selector=kubernetes.meta.v1.LabelSelectorArgs(
                            match_labels={"kubernetes.io/metadata.name": "grafana"},
                        )
                    )
                ],
                ports=[
                    kubernetes.networking.v1.NetworkPolicyPortArgs(
                        port=CLICKHOUSE_METRICS_PORT
                    ),
                ],
            ),
        ],
    ),
)

############################################################
# Monitoring — ServiceMonitor for Prometheus Operator
#
# ClickHouse serves Prometheus metrics at /metrics on CLICKHOUSE_METRICS_PORT
# (9363), and only because the CHI's prometheus/* settings turn it on. It does
# not serve them on the HTTP port (8123); a ServiceMonitor pointed there
# scraped up=0. The ServiceMonitor selects the ``clickhouse`` Service, and
# Prometheus scrapes each replica's endpoint behind it individually.
# Requires the Prometheus Operator (monitoring.coreos.com/v1 CRDs) to be
# installed in the cluster (already present per EKS infrastructure stack).
############################################################
clickhouse_service_monitor = kubernetes.apiextensions.CustomResource(
    f"clickhouse-service-monitor-{stack_info.env_suffix}",
    api_version="monitoring.coreos.com/v1",
    kind="ServiceMonitor",
    metadata=kubernetes.meta.v1.ObjectMetaArgs(
        name="clickhouse",
        namespace=CLICKHOUSE_NAMESPACE,
        labels={
            **k8s_global_labels,
            # Label required for Prometheus Operator to discover this ServiceMonitor
            "release": "prometheus",
        },
    ),
    spec={
        "selector": {
            "matchLabels": {"app": "clickhouse"},
        },
        "namespaceSelector": {"matchNames": [CLICKHOUSE_NAMESPACE]},
        "endpoints": [
            {
                "port": "metrics",
                "path": "/metrics",
                "scheme": "http",
                "interval": "30s",
                "scrapeTimeout": "10s",
                "relabelings": [
                    {
                        "sourceLabels": ["__meta_kubernetes_pod_name"],
                        "targetLabel": "pod",
                    },
                    {
                        "sourceLabels": ["__meta_kubernetes_namespace"],
                        "targetLabel": "namespace",
                    },
                ],
            }
        ],
    },
    opts=ResourceOptions(depends_on=[clickhouse_client_service]),
)

keeper_service_monitor = kubernetes.apiextensions.CustomResource(
    f"clickhouse-keeper-service-monitor-{stack_info.env_suffix}",
    api_version="monitoring.coreos.com/v1",
    kind="ServiceMonitor",
    metadata=kubernetes.meta.v1.ObjectMetaArgs(
        name="clickhouse-keeper",
        namespace=CLICKHOUSE_NAMESPACE,
        labels={**k8s_global_labels, "release": "prometheus"},
    ),
    spec={
        "selector": {"matchLabels": {"app": "clickhouse-keeper"}},
        "namespaceSelector": {"matchNames": [CLICKHOUSE_NAMESPACE]},
        "endpoints": [
            {
                "port": "metrics",
                "path": "/metrics",
                "interval": "30s",
                "scrapeTimeout": "10s",
                # pod_name, container_name and app are the labels Altinity's
                # Keeper dashboard (grafana_alerting/dashboards) filters on.
                # container_name is a constant rather than taken from
                # __meta_kubernetes_pod_container_name: that meta label is only
                # set when the scraped port is a declared container port, and
                # the operator's Keeper pod declares only 2181 and 9444.
                "relabelings": [
                    {
                        "sourceLabels": ["__meta_kubernetes_pod_name"],
                        "targetLabel": "pod",
                    },
                    {
                        "sourceLabels": ["__meta_kubernetes_pod_name"],
                        "targetLabel": "pod_name",
                    },
                    {
                        "targetLabel": "container_name",
                        "replacement": "clickhouse-keeper",
                    },
                    {"targetLabel": "app", "replacement": "clickhouse-keeper"},
                ],
            }
        ],
    },
    opts=ResourceOptions(depends_on=[keeper_metrics_service]),
)

export("clickhouse_namespace", CLICKHOUSE_NAMESPACE)
export(
    "clickhouse_service",
    f"clickhouse.{CLICKHOUSE_NAMESPACE}.svc.cluster.local",
)
export("clickhouse_native_port", 9000)
export("clickhouse_http_port", 8123)
export(
    "keeper_service",
    f"keeper-clickhouse.{CLICKHOUSE_NAMESPACE}.svc.cluster.local",
)
export("cold_storage_bucket", cold_bucket_name)
export(
    "connection_guide",
    dedent(f"""\
        ClickHouse HTTP: http://clickhouse.{CLICKHOUSE_NAMESPACE}.svc.cluster.local:8123
        ClickHouse Native: clickhouse.{CLICKHOUSE_NAMESPACE}.svc.cluster.local:9000
        Keeper: keeper-clickhouse.{CLICKHOUSE_NAMESPACE}.svc.cluster.local:2181

        Tenants: opik_db
        Hot/cold cutoff: {hot_data_days} days (table TTL MOVE expressions)
        Cold storage: s3://{cold_bucket_name}/data/
    """),
)
