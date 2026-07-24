#!/usr/bin/env bash
# Apply the kubelet image-GC thresholds from local-dev/cluster/k3d-config.yaml
# to an EXISTING local-dev cluster. New clusters get them automatically at
# creation; k3s flags can't be changed on a live cluster, so this writes them
# to each node's /etc/rancher/k3s/config.yaml (read at k3s startup — k3d
# passes no competing CLI kubelet-args — and survives node restarts) and
# rolling-restarts the nodes.
#
# Expect a few minutes of churn as each node's pods restart. Images persist
# in the node volumes, so nothing re-pulls. Don't run mid-build.
#
# DELETE ME once the handful of clusters that predate PR #5102 have migrated
# (tracked in witan: tk-local-dev-delete-migrate-kubelet-gc-thresholds-s-1576b6).
# Anyone creating a cluster after that PR merged never needs this script.
#
# Usage: ./local-dev/scripts/migrate-kubelet-gc-thresholds.sh
set -euo pipefail

CTX=k3d-local-dev
NODES=(k3d-local-dev-agent-0 k3d-local-dev-agent-1 k3d-local-dev-server-0)

for node in "${NODES[@]}"; do
    echo "▶ ${node}: writing kubelet GC thresholds to /etc/rancher/k3s/config.yaml"
    # k3d nodes don't ship this file, but back up any existing one rather
    # than silently discarding hand-added config.
    docker exec "$node" sh -c 'if [ -s /etc/rancher/k3s/config.yaml ]; then
        cp /etc/rancher/k3s/config.yaml /etc/rancher/k3s/config.yaml.pre-gc-thresholds.bak
        echo "  (existing config.yaml backed up to config.yaml.pre-gc-thresholds.bak — its settings are no longer applied)"
    fi
    printf "kubelet-arg:\n  - image-gc-high-threshold=75\n  - image-gc-low-threshold=70\n" > /etc/rancher/k3s/config.yaml'
    echo "▶ ${node}: restarting (pods on this node will restart)"
    docker restart "$node" >/dev/null
    printf "  waiting for %s to be Ready" "$node"
    for _ in $(seq 1 60); do
        if kubectl --context "$CTX" wait --for=condition=Ready "node/${node}" \
            --timeout=10s >/dev/null 2>&1; then
            echo " ✓"
            break
        fi
        printf "."
        sleep 5
    done
done

echo ""
echo "▶ verifying thresholds took effect (expect 75 / 70 on every node):"
for node in "${NODES[@]}"; do
    kubectl --context "$CTX" get --raw "/api/v1/nodes/${node}/proxy/configz" \
        | jq --arg n "$node" '.kubeletconfig | {node: $n, imageGCHighThresholdPercent, imageGCLowThresholdPercent}'
done
