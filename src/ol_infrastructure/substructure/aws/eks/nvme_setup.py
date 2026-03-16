"""Set up local NVMe storage for io-optimized EKS node groups.

Deploys two resources onto the io-optimized node group:

1. **NVMe init DaemonSet** — a privileged DaemonSet that runs on every
   ``ol.mit.edu/io_optimized=true`` node.  Each pod formats (if needed) and
   mounts the instance-store NVMe device (``/dev/nvme1n1``) at ``/mnt/nvme``
   on the *host* filesystem via ``nsenter``, then sleeps indefinitely to keep
   the pod alive.

2. **local-path-provisioner Helm release** — Rancher's local-path-provisioner,
   configured to use ``/mnt/nvme/`` as its storage root on io-optimized nodes.
   Creates the ``local-nvme`` StorageClass that ClickHouse (and StarRocks) hot
   volumes reference.

Both resources carry a toleration for the ``ol.mit.edu/io-workload:NoSchedule``
taint so they land exclusively on the io-optimized node group.
"""

import pulumi_kubernetes as kubernetes
from pulumi import ResourceOptions, StackReference

from bridge.lib.versions import LOCAL_PATH_PROVISIONER_CHART_VERSION
from ol_infrastructure.lib.pulumi_helper import require_stack_output_value

IO_OPTIMIZED_NODE_SELECTOR = {"ol.mit.edu/io_optimized": "true"}
IO_OPTIMIZED_TOLERATION = kubernetes.core.v1.TolerationArgs(
    key="ol.mit.edu/io-workload",
    operator="Equal",
    value="true",
    effect="NoSchedule",
)

# Shell script executed by the privileged init container.
# Uses nsenter to operate in the host mount namespace so that the mount
# persists after the container exits.
_NVME_SETUP_SCRIPT = """\
#!/bin/bash
set -euo pipefail
DEVICE="/dev/nvme1n1"
MOUNT_POINT="/mnt/nvme"

# Skip gracefully on nodes without an instance-store device (CI / EBS nodes).
if [ ! -b "$DEVICE" ]; then
  echo "Device $DEVICE not found — skipping NVMe setup"
  exit 0
fi

# Operate in the host mount namespace so mounts persist on the node.
NSENTER="nsenter --mount=/host/proc/1/ns/mnt --"

# Already mounted?
if $NSENTER mountpoint -q "$MOUNT_POINT"; then
  echo "NVMe already mounted at $MOUNT_POINT"
  exit 0
fi

# Format if no filesystem signature present.
if ! $NSENTER blkid "$DEVICE" &>/dev/null; then
  echo "Formatting $DEVICE as xfs…"
  $NSENTER mkfs.xfs -f "$DEVICE"
fi

# Mount.
$NSENTER mkdir -p "$MOUNT_POINT"
$NSENTER mount -o noatime,nodiratime "$DEVICE" "$MOUNT_POINT"
echo "NVMe mounted at $MOUNT_POINT"
"""


def setup_nvme_local_storage(
    cluster_name: str,
    cluster_stack: StackReference,
    k8s_provider: kubernetes.Provider,
    k8s_labels: dict[str, str],
) -> kubernetes.helm.v3.Release | None:
    """Deploy NVMe init DaemonSet and local-path-provisioner for io-optimized nodes.

    Args:
        cluster_name: EKS cluster name, used for Pulumi resource naming.
        cluster_stack: StackReference to the EKS infrastructure stack.
        k8s_provider: Kubernetes provider for the target cluster.
        k8s_labels: Common Pulumi-managed labels applied to all resources.

    Returns:
        The local-path-provisioner Helm Release, or None if disabled.
    """
    stateful_workload_storage = require_stack_output_value(
        cluster_stack, "stateful_workload_storage"
    )
    if not stateful_workload_storage["use_io_optimized_nodes"]:
        return None

    opts = ResourceOptions(provider=k8s_provider, delete_before_replace=True)

    # ------------------------------------------------------------------
    # ConfigMap holding the NVMe init script
    # ------------------------------------------------------------------
    nvme_script_cm = kubernetes.core.v1.ConfigMap(
        f"{cluster_name}-nvme-init-script",
        metadata=kubernetes.meta.v1.ObjectMetaArgs(
            name="nvme-init-script",
            namespace="kube-system",
            labels=k8s_labels,
        ),
        data={"nvme-setup.sh": _NVME_SETUP_SCRIPT},
        opts=opts,
    )

    # ------------------------------------------------------------------
    # Privileged DaemonSet — formats and mounts NVMe on io-optimized nodes
    # ------------------------------------------------------------------
    nvme_daemonset = kubernetes.apps.v1.DaemonSet(
        f"{cluster_name}-nvme-init-daemonset",
        metadata=kubernetes.meta.v1.ObjectMetaArgs(
            name="nvme-init",
            namespace="kube-system",
            labels={**k8s_labels, "app": "nvme-init"},
        ),
        spec=kubernetes.apps.v1.DaemonSetSpecArgs(
            selector=kubernetes.meta.v1.LabelSelectorArgs(
                match_labels={"app": "nvme-init"},
            ),
            template=kubernetes.core.v1.PodTemplateSpecArgs(
                metadata=kubernetes.meta.v1.ObjectMetaArgs(
                    labels={**k8s_labels, "app": "nvme-init"},
                ),
                spec=kubernetes.core.v1.PodSpecArgs(
                    host_pid=True,  # Required for nsenter to the host mount namespace
                    node_selector=IO_OPTIMIZED_NODE_SELECTOR,
                    tolerations=[IO_OPTIMIZED_TOLERATION],
                    init_containers=[
                        kubernetes.core.v1.ContainerArgs(
                            name="nvme-setup",
                            image="public.ecr.aws/amazonlinux/amazonlinux:2023",
                            command=["bash", "/scripts/nvme-setup.sh"],
                            security_context=kubernetes.core.v1.SecurityContextArgs(
                                privileged=True,
                            ),
                            volume_mounts=[
                                kubernetes.core.v1.VolumeMountArgs(
                                    name="scripts",
                                    mount_path="/scripts",
                                    read_only=True,
                                ),
                                kubernetes.core.v1.VolumeMountArgs(
                                    name="host-proc",
                                    mount_path="/host/proc",
                                    read_only=True,
                                ),
                                kubernetes.core.v1.VolumeMountArgs(
                                    name="host-dev",
                                    mount_path="/dev",
                                ),
                            ],
                        )
                    ],
                    containers=[
                        kubernetes.core.v1.ContainerArgs(
                            name="pause",
                            image="public.ecr.aws/eks-distro/kubernetes/pause:3.9",
                            resources=kubernetes.core.v1.ResourceRequirementsArgs(
                                requests={"cpu": "1m", "memory": "4Mi"},
                                limits={"memory": "8Mi"},
                            ),
                        )
                    ],
                    volumes=[
                        kubernetes.core.v1.VolumeArgs(
                            name="scripts",
                            config_map=kubernetes.core.v1.ConfigMapVolumeSourceArgs(
                                name="nvme-init-script",
                                default_mode=0o755,
                            ),
                        ),
                        kubernetes.core.v1.VolumeArgs(
                            name="host-proc",
                            host_path=kubernetes.core.v1.HostPathVolumeSourceArgs(
                                path="/proc",
                            ),
                        ),
                        kubernetes.core.v1.VolumeArgs(
                            name="host-dev",
                            host_path=kubernetes.core.v1.HostPathVolumeSourceArgs(
                                path="/dev",
                            ),
                        ),
                    ],
                ),
            ),
        ),
        opts=ResourceOptions(
            provider=k8s_provider,
            depends_on=[nvme_script_cm],
        ),
    )

    # ------------------------------------------------------------------
    # local-path-provisioner — creates the ``local-nvme`` StorageClass
    # ------------------------------------------------------------------
    return kubernetes.helm.v3.Release(
        f"{cluster_name}-local-path-provisioner",
        kubernetes.helm.v3.ReleaseArgs(
            name="local-path-provisioner",
            chart="local-path-provisioner",
            version=LOCAL_PATH_PROVISIONER_CHART_VERSION,
            namespace="kube-system",
            cleanup_on_fail=True,
            skip_await=False,
            repository_opts=kubernetes.helm.v3.RepositoryOptsArgs(
                repo="https://charts.rancher.io",
            ),
            values={
                "storageClass": {
                    "name": "local-nvme",
                    "defaultClass": False,
                    "reclaimPolicy": "Delete",
                },
                "nodePathMap": [
                    {
                        "node": "DEFAULT_PATH_FOR_NON_LISTED_NODES",
                        "paths": ["/mnt/nvme"],
                    }
                ],
                "nodeSelector": IO_OPTIMIZED_NODE_SELECTOR,
                "tolerations": [
                    {
                        "key": "ol.mit.edu/io-workload",
                        "operator": "Equal",
                        "value": "true",
                        "effect": "NoSchedule",
                    }
                ],
                "resources": {
                    "requests": {"cpu": "50m", "memory": "64Mi"},
                    "limits": {"memory": "128Mi"},
                },
            },
        ),
        opts=ResourceOptions(
            provider=k8s_provider,
            depends_on=[nvme_daemonset],
        ),
    )
