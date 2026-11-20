"""Meilisearch Helm release installation and configuration for EDXApp."""

import hashlib
import hmac
import json
from pathlib import Path
from typing import Any

import pulumi_kubernetes as kubernetes
from pulumi import Config, ResourceOptions

from bridge.lib.versions import MEILISEARCH_CHART_VERSION, MEILISEARCH_VERSION
from bridge.secrets.sops import read_yaml_secrets
from ol_infrastructure.applications.edxapp.k8s_configmaps import browser_origins
from ol_infrastructure.components.services.apisix import (
    OLApisixSharedPlugins,
    OLApisixSharedPluginsConfig,
)
from ol_infrastructure.components.services.apisix_gateway_api import (
    OLApisixHTTPRoute,
    OLApisixHTTPRouteConfig,
)
from ol_infrastructure.components.services.cert_manager import (
    OLCertManagerCert,
    OLCertManagerCertConfig,
)
from ol_infrastructure.lib.pulumi_helper import StackInfo

# Meilisearch computes an API key as HMAC-SHA256(master key, uid)
# (generate_key_as_hexa in crates/meilisearch-auth/src/store.rs), so a key
# created with a fixed uid has the same value in every database that shares the
# master key. edxapp used to carry a copy of the Default Admin API Key, whose
# uid is random per database, and recreating the database silently invalidated
# it (mitx CI, 2026-04-01 to 2026-10-01). The uid is not secret: anyone holding
# a key can read it from GET /keys, and the value still needs the master key.
EDXAPP_MEILISEARCH_KEY_UID = "be0444d7-0b85-4dde-a512-0fd3e205902a"

# Same scope as the Default Admin API Key edxapp used before. edx-platform
# manages indexes, writes documents, and signs tenant tokens with this key, and
# it calls GET /keys/<key> to look up the uid it signs them with.
EDXAPP_MEILISEARCH_KEY_REQUEST = {
    "uid": EDXAPP_MEILISEARCH_KEY_UID,
    "name": "edxapp",
    "description": "Open edX Studio content search. Managed by ol-infrastructure.",
    "actions": ["*"],
    "indexes": ["*"],
    "expiresAt": None,
}

EDXAPP_KEY_JOB_NAME = "meilisearch-edxapp-key"

# How quickly a recreated database gets the edxapp key back.
EDXAPP_KEY_RECONCILE_SCHEDULE = "*/15 * * * *"

EDXAPP_KEY_JOB_TTL_SECONDS = 3600

# Meilisearch answers 201 when it creates the key and 409 (api_key_already_exists)
# when a key with this uid is already there, so the script is safe to repeat.
# The master key goes to curl on stdin so it stays out of the process arguments.
_ENSURE_EDXAPP_KEY_SCRIPT = """\
code=$(printf 'Authorization: Bearer %s\\n' "$MEILI_MASTER_KEY" | curl -sS \\
  -o /tmp/response -w '%{http_code}' -H @- -H 'Content-Type: application/json' \\
  --data "$KEY_REQUEST" http://meilisearch:7700/keys)
case "$code" in
  201|409) echo "edxapp Meilisearch key present (HTTP $code)" ;;
  *) echo "creating the edxapp Meilisearch key failed (HTTP $code)"
     cat /tmp/response
     exit 1 ;;
esac
"""


def edxapp_meilisearch_api_key(master_key: str) -> str:
    """Compute the value Meilisearch gives the edxapp key under ``master_key``.

    :param master_key: the Meilisearch master key.
    :returns: the API key edxapp authenticates with.
    :rtype: str
    """
    return hmac.new(
        master_key.encode(), EDXAPP_MEILISEARCH_KEY_UID.encode(), hashlib.sha256
    ).hexdigest()


def _create_edxapp_key_jobs(
    stack_info: StackInfo,
    namespace: str,
    k8s_global_labels: dict[str, str],
    master_key_secret_name: str,
    meilisearch_release: kubernetes.helm.v3.Release,
) -> None:
    """Create the edxapp API key in Meilisearch, and recreate it when it goes missing.

    The Job creates the key as soon as the release is up, so a deploy that
    switches edxapp to the derived key does not leave Studio with a key
    Meilisearch has never seen. The CronJob covers a database recreated later
    (a replaced PVC starts with only the default keys).
    """
    labels = {**k8s_global_labels, "app.kubernetes.io/name": EDXAPP_KEY_JOB_NAME}
    job_spec = kubernetes.batch.v1.JobSpecArgs(
        backoff_limit=2,
        active_deadline_seconds=300,
        ttl_seconds_after_finished=EDXAPP_KEY_JOB_TTL_SECONDS,
        template=kubernetes.core.v1.PodTemplateSpecArgs(
            metadata=kubernetes.meta.v1.ObjectMetaArgs(labels=labels),
            spec=kubernetes.core.v1.PodSpecArgs(
                restart_policy="Never",
                automount_service_account_token=False,
                security_context=kubernetes.core.v1.PodSecurityContextArgs(
                    run_as_non_root=True,
                    run_as_user=1000,
                    run_as_group=1000,
                ),
                containers=[
                    kubernetes.core.v1.ContainerArgs(
                        name="ensure-edxapp-key",
                        # The Meilisearch image ships curl, so this needs no
                        # image of its own.
                        image=f"getmeili/meilisearch:{MEILISEARCH_VERSION}",
                        command=["/bin/sh", "-c", _ENSURE_EDXAPP_KEY_SCRIPT],
                        env=[
                            kubernetes.core.v1.EnvVarArgs(
                                name="MEILI_MASTER_KEY",
                                value_from=kubernetes.core.v1.EnvVarSourceArgs(
                                    secret_key_ref=kubernetes.core.v1.SecretKeySelectorArgs(
                                        name=master_key_secret_name,
                                        key="MEILI_MASTER_KEY",
                                    ),
                                ),
                            ),
                            kubernetes.core.v1.EnvVarArgs(
                                name="KEY_REQUEST",
                                value=json.dumps(EDXAPP_MEILISEARCH_KEY_REQUEST),
                            ),
                        ],
                        security_context=kubernetes.core.v1.SecurityContextArgs(
                            allow_privilege_escalation=False,
                            capabilities=kubernetes.core.v1.CapabilitiesArgs(
                                drop=["ALL"]
                            ),
                        ),
                        resources=kubernetes.core.v1.ResourceRequirementsArgs(
                            requests={"cpu": "10m", "memory": "16Mi"},
                            limits={"memory": "64Mi"},
                        ),
                    )
                ],
            ),
        ),
    )

    kubernetes.batch.v1.Job(
        f"ol-{stack_info.env_prefix}-edxapp-meilisearch-key-job-{stack_info.env_suffix}",
        metadata=kubernetes.meta.v1.ObjectMetaArgs(
            namespace=namespace,
            labels=labels,
        ),
        spec=job_spec,
        opts=ResourceOptions(depends_on=[meilisearch_release]),
    )
    kubernetes.batch.v1.CronJob(
        f"ol-{stack_info.env_prefix}-edxapp-meilisearch-key-cronjob-{stack_info.env_suffix}",
        metadata=kubernetes.meta.v1.ObjectMetaArgs(
            name=EDXAPP_KEY_JOB_NAME,
            namespace=namespace,
            labels=labels,
        ),
        spec=kubernetes.batch.v1.CronJobSpecArgs(
            schedule=EDXAPP_KEY_RECONCILE_SCHEDULE,
            concurrency_policy="Forbid",
            starting_deadline_seconds=300,
            successful_jobs_history_limit=1,
            failed_jobs_history_limit=3,
            job_template=kubernetes.batch.v1.JobTemplateSpecArgs(
                metadata=kubernetes.meta.v1.ObjectMetaArgs(labels=labels),
                spec=job_spec,
            ),
        ),
        opts=ResourceOptions(depends_on=[meilisearch_release]),
    )


def create_meilisearch_resources(
    stack_info: StackInfo,
    namespace: str,
    k8s_global_labels: dict[str, str],
) -> kubernetes.helm.v3.Release | None:
    """Create Meilisearch Helm release if enabled in configuration.

    Args:
        stack_info: Stack information with environment details
        namespace: Kubernetes namespace where Meilisearch will be deployed

    Returns:
        Meilisearch Helm release resource, or None if not enabled
    """
    meilisearch_config = Config("meilisearch")
    if not meilisearch_config.get_bool("deploy"):
        return None

    tls_secret_name = "meilisearch-tls-pair"  # pragma: allowlist secret  # noqa: S105
    OLCertManagerCert(
        f"ol-{stack_info.env_prefix}-edxapp-meilisearch-cert-{stack_info.env_suffix}",
        cert_config=OLCertManagerCertConfig(
            application_name="meilisearch",
            k8s_namespace=namespace,
            k8s_labels=k8s_global_labels,
            create_apisixtls_resource=True,
            dest_secret_name=tls_secret_name,
            dns_names=[meilisearch_config.require("domain")],
        ),
    )

    meilisearch_shared_plugins = OLApisixSharedPlugins(
        f"ol-{stack_info.env_prefix}-edxapp-meilisearch-shared-plugins-{stack_info.env_suffix}",
        plugin_config=OLApisixSharedPluginsConfig(
            application_name="meilisearch",
            resource_suffix="ol-shared-plugins",
            render_only=True,
            k8s_namespace=namespace,
            k8s_labels=k8s_global_labels,
            # Meilisearch is queried straight from the browser by Open edX's
            # course-search MFE, which runs on the LMS and Studio origins, so it
            # needs the same page-origin list edxapp itself allows.
            cors_allow_origins=browser_origins(stack_info, Config("edxapp")),
        ),
    )

    OLApisixHTTPRoute(
        f"ol-{stack_info.env_prefix}-edxapp-meilisearch-httproute-{stack_info.env_suffix}",
        k8s_namespace=namespace,
        k8s_labels=k8s_global_labels,
        route_configs=[
            OLApisixHTTPRouteConfig(
                route_name="meilisearch",
                hosts=[meilisearch_config.require("domain")],
                paths=["/*"],
                backend_service_name="meilisearch",
                backend_service_port=7700,
                shared_plugins=meilisearch_shared_plugins,
            ),
        ],
    )

    secrets = read_yaml_secrets(
        Path(f"edxapp/{stack_info.env_prefix}.{stack_info.env_suffix}.yaml")
    )

    # The chart renders `environment` into a ConfigMap, so the master key goes in
    # a Secret that the chart mounts through auth.existingMasterKeySecret. The
    # chart's ServiceMonitor reads its bearer token from the same Secret, and the
    # chart only creates that Secret itself when no master key is supplied.
    master_key_secret_name = "meilisearch-master-key"  # pragma: allowlist secret  # noqa: S105
    master_key_secret = kubernetes.core.v1.Secret(
        f"ol-{stack_info.env_prefix}-edxapp-meilisearch-master-key-{stack_info.env_suffix}",
        metadata=kubernetes.meta.v1.ObjectMetaArgs(
            name=master_key_secret_name,
            namespace=namespace,
            labels=k8s_global_labels,
        ),
        string_data={
            "MEILI_MASTER_KEY": secrets["meilisearch_master_key"],
        },
    )

    meilisearch_values: dict[str, Any] = {
        "auth": {
            "existingMasterKeySecret": master_key_secret_name,
        },
        "replicaCount": meilisearch_config.get_int("replica_count")
        or 1,  # Default to 1 replica
        "image": {
            "tag": MEILISEARCH_VERSION,
            "pullPolicy": "IfNotPresent",
        },
        "customLabels": k8s_global_labels,
        "environment": {
            "MEILI_NO_ANALYTICS": True,
            "MEILI_ENV": "production",
            # Without this, Meilisearch refuses to start whenever the on-disk
            # database was written by a different engine version, which is what
            # forced the v1.33.0 image pin in March 2026. It migrates the
            # database in place on startup and is a no-op once the versions
            # match, so it is safe to leave enabled permanently. Note that this
            # is one-way: Meilisearch has no downgrade path, so rolling the
            # image tag back requires restoring the volume from a snapshot.
            "MEILI_UPGRADE_DB": True,
        },
        # The chart's default startup budget is 60s (60 x 1s). A version
        # upgrade migrates the task queue synchronously before the HTTP server
        # binds, so that default can kill the pod mid-migration and leave the
        # database partially converted. Give it 15 minutes instead.
        "startupProbe": {
            "periodSeconds": 5,
            "failureThreshold": 180,
        },
        "persistence": {
            "enabled": True,
            "size": meilisearch_config.get("pv_size") or "10Gi",
        },
        # The chart only rolls the pod on a ConfigMap change, and the key no longer
        # lives there, so without this a rotated key would not reach the running
        # process until someone restarted it by hand.
        "podAnnotations": {
            "checksum/master-key": hashlib.sha256(
                secrets["meilisearch_master_key"].encode()
            ).hexdigest(),
        },
        # Enabling this also sets MEILI_EXPERIMENTAL_ENABLE_METRICS in the chart's
        # ConfigMap. /metrics needs a key, hence the Secret above. The CI clusters
        # run no metrics collector, so there is nothing to scrape it there.
        "serviceMonitor": {
            "enabled": stack_info.env_suffix != "ci",
            # Same discovery label as the dagster and clickhouse ServiceMonitors
            "additionalLabels": {"release": "prometheus"},
        },
        "resources": {
            "requests": {
                "cpu": meilisearch_config.get("cpu_request") or "250m",
                "memory": meilisearch_config.get("memory_request") or "512Mi",
            },
            "limits": {
                # Don't set a CPU limit per our standard practice
                "memory": meilisearch_config.get("memory_limit") or "512Mi",
            },
        },
        # Pin to the static on-demand core nodegroup instead of the
        # Karpenter spot fleet: its PVC is zone-locked once bound, and spot
        # rebalance-recommendation waves were cordoning nodes faster than
        # the pod could reach Ready (2026-07-27 TMM).
        "nodeSelector": {
            "ol.mit.edu/core_node": "true",
        },
    }

    # Meilisearch sizes its indexing buffer at two thirds of the machine's total
    # memory, measured with the sysinfo crate, which reads the host's
    # /proc/meminfo and not the cgroup limit. On the m8i-flex.4xlarge core nodes
    # that means it budgets ~40Gi inside whatever memory_limit we set, so it
    # never flushes early and leaves the cgroup to reclaim instead. Pin it so
    # the budget and the limit agree. Only emitted when configured, so the
    # stacks that have not been sized for it render unchanged.
    if max_indexing_memory := meilisearch_config.get("max_indexing_memory"):
        meilisearch_values["environment"]["MEILI_MAX_INDEXING_MEMORY"] = (
            max_indexing_memory
        )

    meilisearch_release = kubernetes.helm.v3.Release(
        f"ol-{stack_info.env_prefix}-edxapp-meilisearch-helm-release-{stack_info.env_suffix}",
        kubernetes.helm.v3.ReleaseArgs(
            name="meilisearch",
            chart="meilisearch",
            version=MEILISEARCH_CHART_VERSION,
            namespace=namespace,
            cleanup_on_fail=True,
            max_history=10,
            repository_opts=kubernetes.helm.v3.RepositoryOptsArgs(
                repo="https://meilisearch.github.io/meilisearch-kubernetes",
            ),
            values=meilisearch_values,
            skip_await=False,
        ),
        opts=ResourceOptions(
            delete_before_replace=True, depends_on=[master_key_secret]
        ),
    )

    _create_edxapp_key_jobs(
        stack_info,
        namespace,
        k8s_global_labels,
        master_key_secret_name,
        meilisearch_release,
    )

    # Raise the throughput of an already-provisioned volume. The storageclass sets
    # gp3 throughput at provisioning time and cannot revise it afterwards, so a
    # volume whose index has outgrown the pod's page cache stays pinned at whatever
    # ceiling it was born with. Pointing the PVC at a VolumeAttributesClass drives
    # ec2:ModifyVolume against the live volume instead -- online, no pod restart, no
    # rebind, so it does not disturb the zone-locked PVC.
    #
    # The chart's PVC template has no field for this (meilisearch-kubernetes 0.38.0),
    # hence a patch against the Helm-created claim rather than a values entry.
    # Emitted only where configured, so the stacks that have not been sized for it
    # render unchanged.
    if volume_attributes_class := meilisearch_config.get("volume_attributes_class"):
        kubernetes.core.v1.PersistentVolumeClaimPatch(
            f"ol-{stack_info.env_prefix}-edxapp-meilisearch-pvc-attributes-{stack_info.env_suffix}",
            metadata=kubernetes.meta.v1.ObjectMetaPatchArgs(
                name="meilisearch",
                namespace=namespace,
                labels=k8s_global_labels,
                annotations={"pulumi.com/patchForce": "true"},
            ),
            spec=kubernetes.core.v1.PersistentVolumeClaimSpecPatchArgs(
                volume_attributes_class_name=volume_attributes_class,
            ),
            opts=ResourceOptions(depends_on=[meilisearch_release]),
        )

    return meilisearch_release
