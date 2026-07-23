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

container_running() {
    docker ps --format '{{.Names}}' | grep -qx "$1"
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
# 2. k3d registry — the registry has no default expiry, so old pushes (every
#    Tilt build, forever) just accumulate as blobs. There's nothing worth
#    keeping in this cache: Tilt re-pushes whatever the current build needs
#    on the next `tilt up`, from its own local build cache, so wiping it is
#    cheap to recover from.
# ---------------------------------------------------------------------------
if container_running "$REGISTRY_CONTAINER"; then
    log "Clearing local registry storage (${REGISTRY_CONTAINER})..."
    docker exec "$REGISTRY_CONTAINER" sh -c \
        'rm -rf /var/lib/registry/docker/registry/v2/blobs/* /var/lib/registry/docker/registry/v2/repositories/*'
    # The registry's in-memory blob-descriptor cache still believes the wiped
    # blobs exist; without a restart it answers the next push's existence
    # checks with "layer already exists", producing manifests whose blobs
    # were never re-uploaded (pods then fail pulls with "unexpected EOF" —
    # this corrupted the registry during the 2026-07-23 incident). If the
    # restart fails, the stale cache is live over wiped storage — the exact
    # incident pre-condition — so stop hard rather than continue.
    if ! docker restart "$REGISTRY_CONTAINER" >/dev/null; then
        warn "REGISTRY RESTART FAILED — do NOT push (Tilt included) until"
        warn "'docker restart ${REGISTRY_CONTAINER}' succeeds, or the next"
        warn "push will silently corrupt the registry (stale blob cache)."
        exit 1
    fi
    ok "Registry storage cleared (registry restarted to drop its blob cache)."
else
    warn "${REGISTRY_CONTAINER} not running — skipping registry cleanup."
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
