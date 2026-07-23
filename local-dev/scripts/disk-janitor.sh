#!/usr/bin/env bash
# disk-janitor.sh — keep the local-dev Docker/k3d disk footprint bounded.
#
# Tilt's built-in pruner can't be relied on for this: it has silent failure
# modes (a Docker hiccup at `tilt up` disables it for the whole session, and
# every per-image skip is Debug-level only), and it can never reach the k3d
# registry (tilt-dev/tilt#2102) or the k3s nodes' containerd stores
# (tilt-dev/tilt#4228). Left alone, those stores grow by ~36GB per app
# rebuild until kubelet taints every node with disk-pressure and nothing
# schedules.
#
# This janitor enforces a retention policy instead of reacting to low disk.
# Retention (keep the newest N tags, plus anything a pod references) is safe
# to run at any moment — unlike a wipe, it can never delete an image
# something is about to need. It runs as a Tilt serve_cmd resource, so it is
# alive exactly when builds (the only source of growth) can happen.
#
#   1. Local Docker daemon: keep the newest N tilt-built tags per repo
#      (localhost:5001/*); remove older ones. Never touches other images.
#   2. k3d registry: delete tags that are neither kept locally nor referenced
#      by any pod, then garbage-collect blobs (skipped while a push is in
#      flight).
#   3. Docker build cache: prune down to a size cap, least-recently-used
#      first. NOTE: the BuildKit cache is one daemon-wide pool shared with
#      everything else you build on this machine, so this is the one step
#      whose effect is not scoped to local-dev (the cost is only rebuild
#      speed, never correctness). Set the cap to 0 to opt out and manage the
#      pool yourself (e.g. daemon builder.gc config).
#
# The k3s nodes' internal containerd stores are intentionally NOT handled
# here — kubelet's own image GC owns those (thresholds are set in
# local-dev/cluster/k3d-config.yaml).
#
# Knobs (wired from tilt_config.json / env by the root Tiltfile):
#   JANITOR_KEEP_TAGS          newest tags kept per repo (default 3)
#   JANITOR_BUILDCACHE_MAX_GB  build-cache cap in GB; empty = 10% of total
#                              disk; 0 = leave the build cache alone
#   JANITOR_INTERVAL_SECS      loop interval (default 1800); 0 = single pass
#
# Usage: ./local-dev/scripts/disk-janitor.sh   (or let Tilt run it)

set -uo pipefail

KUBE_CONTEXT="${JANITOR_KUBE_CONTEXT:-k3d-local-dev}"
REGISTRY_CONTAINER="k3d-registry.localhost"
REGISTRY_DATA="/var/lib/registry/docker/registry/v2"
# One registry, two identities. k3d maps host port 5001 to the registry
# container's fixed internal port 5000, and k3d's Simple config offers no way
# to unify them — so the same image has two names and both must be treated
# as equivalent when deciding what to keep:
REGISTRY_HOST_REF="localhost:5001"                    # how Tilt/docker on the host name it
REGISTRY_CLUSTER_REF="k3d-registry.localhost:5000"    # how pod specs name it (Tilt rewrites to this)
# k3d-registry.localhost also resolves on the host (via /etc/hosts), so
# accept that spelling of the host-side port too.
REGISTRY_PREFIXES=("$REGISTRY_HOST_REF" "$REGISTRY_CLUSTER_REF" "k3d-registry.localhost:5001")

KEEP_TAGS="${JANITOR_KEEP_TAGS:-3}"
INTERVAL="${JANITOR_INTERVAL_SECS:-1800}"

ts() { date "+%Y-%m-%d %H:%M:%S"; }
log() { echo "[$(ts)] $*"; }

container_running() {
    docker ps --format '{{.Names}}' | grep -qx "$1"
}

# ---------------------------------------------------------------------------
# 1. Local daemon: keep newest KEEP_TAGS tags per localhost:5001/* repo.
# CreatedAt ("2026-07-23 15:17:42 +0000 UTC") sorts correctly lexically.
# `docker rmi` without --force fails (tolerated) on anything still in use.
# ---------------------------------------------------------------------------
prune_local_tags() {
    local removed=0 ref
    while IFS= read -r ref; do
        [[ -z "$ref" ]] && continue
        if docker rmi "$ref" >/dev/null 2>&1; then
            removed=$((removed + 1))
        else
            log "  kept $ref (in use or remove failed)"
        fi
    done < <(
        docker images --filter reference='localhost:5001/*' \
            --format '{{.CreatedAt}}\t{{.Repository}}\t{{.Tag}}' 2>/dev/null \
            | sort -r \
            | awk -F'\t' -v keep="$KEEP_TAGS" '{ if (++seen[$2] > keep) print $2 ":" $3 }'
    )
    (( removed > 0 )) && log "local daemon: removed $removed old tilt tag(s)"
}

# Every image ref the cluster might still pull: live pods (incl. init
# containers) PLUS workload templates. Templates are the durable source of
# truth — during node-restart churn pods can transiently be Failed or absent
# while their Deployment still specifies the tag, and a pods-only keep-set
# deletes a tag the replacement pod then fails to pull. Failed pods are
# excluded (their containers never restart in place; the owning template is
# what matters and is covered); Pending pods count — kubelet still has to
# pull for them.
pod_image_refs() {
    {
        kubectl --context "$KUBE_CONTEXT" get pods -A \
            --field-selector=status.phase!=Failed \
            -o jsonpath='{range .items[*]}{range .spec.initContainers[*]}{.image}{"\n"}{end}{range .spec.containers[*]}{.image}{"\n"}{end}{end}' \
            2>/dev/null || return 1
        kubectl --context "$KUBE_CONTEXT" get deployments,statefulsets,daemonsets -A \
            -o jsonpath='{range .items[*]}{range .spec.template.spec.initContainers[*]}{.image}{"\n"}{end}{range .spec.template.spec.containers[*]}{.image}{"\n"}{end}{end}' \
            2>/dev/null || return 1
    }
}

# Strip any known registry host prefix, yielding "repo:tag" lines.
strip_registry_prefix() {
    local prefix out
    while IFS= read -r line; do
        out=""
        for prefix in "${REGISTRY_PREFIXES[@]}"; do
            if [[ "$line" == "$prefix"/* ]]; then
                out="${line#"$prefix"/}"
                break
            fi
        done
        [[ -n "$out" ]] && echo "$out"
    done
}

# ---------------------------------------------------------------------------
# 2. Registry: a tag is kept iff it survives locally (Tilt may redeploy it)
# or a pod references it (kubelet may re-pull it). Everything else goes.
# Tag dirs are removed in place; blobs are freed by the registry's own
# garbage collector, which we only run when no upload is in flight.
# If kubectl fails we skip the whole phase — fail safe, prune nothing.
# ---------------------------------------------------------------------------
prune_registry() {
    container_running "$REGISTRY_CONTAINER" || return 0

    local pod_refs
    if ! pod_refs="$(pod_image_refs)"; then
        log "registry: kubectl unavailable — skipping registry retention this cycle"
        return 0
    fi

    local keep
    keep="$(
        {
            docker images --filter reference='localhost:5001/*' \
                --format '{{.Repository}}:{{.Tag}}' 2>/dev/null
            echo "$pod_refs"
        } | strip_registry_prefix | sort -u
    )"

    local removed=0 repo tag
    while IFS= read -r repo; do
        [[ -z "$repo" ]] && continue
        while IFS= read -r tag; do
            [[ -z "$tag" ]] && continue
            if ! grep -qx "${repo}:${tag}" <<<"$keep"; then
                docker exec "$REGISTRY_CONTAINER" \
                    rm -rf "${REGISTRY_DATA}/repositories/${repo}/_manifests/tags/${tag}"
                removed=$((removed + 1))
            fi
        done < <(docker exec "$REGISTRY_CONTAINER" \
                    ls "${REGISTRY_DATA}/repositories/${repo}/_manifests/tags" 2>/dev/null)
    done < <(docker exec "$REGISTRY_CONTAINER" \
                ls "${REGISTRY_DATA}/repositories" 2>/dev/null)

    if (( removed > 0 )); then
        log "registry: removed $removed stale tag(s)"
        registry_gc
    fi
}

registry_gc() {
    # An in-flight push writes under _uploads/; GC during a push can corrupt
    # it, so defer to the next cycle if anything is there.
    if docker exec "$REGISTRY_CONTAINER" \
        sh -c "find ${REGISTRY_DATA}/repositories -path '*/_uploads/*' -type f 2>/dev/null | head -1" \
        | grep -q .; then
        log "registry: push in flight — deferring blob GC to next cycle"
        return 0
    fi
    # --delete-untagged (registry >= 2.8) also drops the manifests our tag
    # deletion orphaned; without it blobs stay pinned and GC frees ~nothing.
    if docker exec "$REGISTRY_CONTAINER" registry garbage-collect --help 2>&1 \
        | grep -q -- --delete-untagged; then
        if docker exec "$REGISTRY_CONTAINER" \
            registry garbage-collect --delete-untagged /etc/docker/registry/config.yml \
            >/dev/null 2>&1; then
            # The running registry's in-memory blob-descriptor cache still
            # believes GC'd blobs exist, so it would tell the next `docker
            # push` "layer already exists" and silently produce a manifest
            # with missing blobs (this exact failure corrupted the registry
            # in the 2026-07-23 incident). Restart to drop the cache.
            docker restart "$REGISTRY_CONTAINER" >/dev/null 2>&1
            log "registry: blob GC complete (registry restarted to drop its blob cache)"
        else
            log "registry: blob GC failed (will retry next cycle)"
        fi
    else
        log "registry: 'registry garbage-collect --delete-untagged' unsupported — tags removed but blobs not freed"
    fi
}

# ---------------------------------------------------------------------------
# 3. Build cache: cap the daemon-wide BuildKit pool, LRU eviction.
# ---------------------------------------------------------------------------
prune_build_cache() {
    local cap="${JANITOR_BUILDCACHE_MAX_GB:-}"
    if [[ -z "$cap" ]]; then
        local total_gb
        total_gb="$(df -k / 2>/dev/null | awk 'NR==2 {print int($2/1048576)}')"
        [[ -z "$total_gb" || "$total_gb" -eq 0 ]] && return 0
        cap=$((total_gb / 10))
    fi
    [[ "$cap" == "0" ]] && return 0

    # Flag was renamed --keep-storage -> --max-used-space in newer buildx.
    local flag="--keep-storage"
    docker builder prune --help 2>&1 | grep -q -- --max-used-space && flag="--max-used-space"
    docker builder prune -f "$flag" "${cap}gb" >/dev/null 2>&1 \
        || log "build cache: prune failed"
}

run_cycle() {
    prune_local_tags
    prune_registry
    prune_build_cache
}

log "disk-janitor starting (keep_tags=${KEEP_TAGS}, buildcache_max_gb=${JANITOR_BUILDCACHE_MAX_GB:-auto}, interval=${INTERVAL}s)"

if [[ "$INTERVAL" == "0" ]]; then
    run_cycle
    exit 0
fi

while true; do
    run_cycle
    log "cycle done — next in ${INTERVAL}s"
    sleep "$INTERVAL"
done
