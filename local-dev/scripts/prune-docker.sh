#!/usr/bin/env bash
# prune-docker.sh — BREAK-GLASS full cleanup of the local-dev Docker/k3d disk
# footprint. Day-to-day retention is handled automatically by
# disk-janitor.sh (a Tilt serve_cmd resource); reach for this only when disk
# is critically low or nodes are already tainted with disk-pressure.
#
# Expect fallout: this wipes the registry and sweeps every unused image from
# the k3s nodes, so recovery means kubelet re-pulling infra images from
# public registries and Tilt fully rebuilding the app images. Pods may sit in
# ImagePullBackOff until that completes (this bit us in the 2026-07-23
# incident — pods that were merely Pending counted as "not using" their
# images, so the sweep took currently-needed images with it).
#
# What it cleans:
#   1. Local Docker daemon (tilt-built images, dangling images, build cache)
#   2. The k3d cluster's local registry (every pushed build)
#   3. Each k3d node's containerd image store (all images not in active use)
#
# Usage:
#   ./local-dev/scripts/prune-docker.sh

set -euo pipefail

CLUSTER_NAME="local-dev"
REGISTRY_CONTAINER="k3d-registry.localhost"

log()  { echo "▶ $*"; }
ok()   { echo "  ✓ $*"; }
warn() { echo "  ⚠ $*"; }

container_exists() {
    docker ps -a --format '{{.Names}}' | grep -qx "$1"
}

echo "Disk usage before:"
docker system df
echo ""

# ---------------------------------------------------------------------------
# 1. Local Docker daemon. Image removal is scoped to tilt-built images
#    (localhost:5001/*) plus dangling layers — a global `docker image prune
#    -a` would also take images belonging to unrelated projects on this
#    machine. The build-cache prune IS global (BuildKit keeps one daemon-wide
#    pool; there is no way to scope it), but that only costs other projects
#    rebuild speed.
# ---------------------------------------------------------------------------
log "Pruning local Docker build cache and tilt-built images..."
docker builder prune -af >/dev/null
docker images --filter reference='localhost:5001/*' --format '{{.Repository}}:{{.Tag}}' \
    | xargs -r -n1 docker rmi >/dev/null 2>&1 || true
docker image prune -f >/dev/null
ok "Local Docker daemon pruned."

# ---------------------------------------------------------------------------
# 2. k3d registry — day-to-day, zot's own retention/GC keeps this bounded
#    (local-dev/cluster/zot-config.json); this is the break-glass full wipe.
#    There's nothing worth keeping in this cache: Tilt re-pushes whatever the
#    current build needs on the next `tilt up`, from its own local build
#    cache, so wiping it is cheap to recover from.
#    The wipe is done stopped: deleting storage under a RUNNING registry
#    leaves stale in-memory state that can silently corrupt the next push
#    (registry:2's blob-descriptor cache did exactly that in the 2026-07-23
#    incident — "layer already exists" for blobs that were gone). Paths cover
#    both zot (/var/lib/zot) and pre-migration registry:2 (/var/lib/registry).
# ---------------------------------------------------------------------------
if container_exists "$REGISTRY_CONTAINER"; then
    log "Clearing local registry storage (${REGISTRY_CONTAINER})..."
    docker stop "$REGISTRY_CONTAINER" >/dev/null 2>&1 || true    # may already be stopped
    docker run --rm --volumes-from "$REGISTRY_CONTAINER" alpine sh -c \
        'rm -rf /var/lib/zot/* /var/lib/registry/docker/registry/v2/blobs/* /var/lib/registry/docker/registry/v2/repositories/* 2>/dev/null; true'
    if ! docker start "$REGISTRY_CONTAINER" >/dev/null; then
        warn "REGISTRY FAILED TO START after wipe — run"
        warn "'docker start ${REGISTRY_CONTAINER}' before any push or pull."
        exit 1
    fi
    ok "Registry storage cleared (wiped stopped, restarted clean)."
else
    warn "${REGISTRY_CONTAINER} does not exist — skipping registry cleanup."
fi

# ---------------------------------------------------------------------------
# 3. k3s node containerd stores — OFF by default; pass --sweep-nodes to run.
#
#    kubelet's own image GC normally reclaims these (thresholds in
#    local-dev/cluster/k3d-config.yaml), so this sweep is only for when a
#    node is already tainted and kubelet GC can't catch up.
#
#    WARNING: unlike `docker rmi`, `crictl rmi` deletes the image RECORD even
#    when running containers use the image (containerd containers pin
#    snapshots, not image records). During the 2026-07-23 incident this left
#    every node with an empty image store, ~96GB of orphaned-but-pinned
#    snapshots that containerd's GC could never reclaim, and containers that
#    could not restart without a re-pull from a registry this same script had
#    just wiped. After running this, you MUST let Tilt rebuild + push all app
#    images, then `docker restart` each node one at a time so kubelet
#    re-pulls and re-creates image records.
# ---------------------------------------------------------------------------
if [[ "${1:-}" == "--sweep-nodes" ]]; then
    mapfile -t nodes < <(docker ps --format '{{.Names}}' | grep "^k3d-${CLUSTER_NAME}-\(server\|agent\)-" || true)
    if [[ ${#nodes[@]} -eq 0 ]]; then
        warn "No running k3d nodes found for cluster '${CLUSTER_NAME}' — skipping node image cleanup."
    else
        for node in "${nodes[@]}"; do
            log "Sweeping images on node ${node} (containers will need re-pulls to restart)..."
            # || warn: under set -e a node stopping mid-sweep would otherwise
            # abort the remaining nodes and skip the follow-up instructions.
            if docker exec "$node" sh -c \
                'crictl images -q | xargs -r -n1 sh -c "crictl rmi \"\$0\" 2>/dev/null || true"'; then
                ok "${node} swept."
            else
                warn "${node} sweep failed (node stopped?) — continuing with remaining nodes."
            fi
        done
        warn "Node stores swept. Follow up: let Tilt rebuild+push all app images,"
        warn "then 'docker restart' each k3d node one at a time (see script header)."
    fi
else
    log "Skipping k3s node image stores (kubelet image GC owns those; pass --sweep-nodes to force)."
fi

echo ""
echo "Disk usage after:"
docker system df
