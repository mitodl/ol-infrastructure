#!/usr/bin/env bash
# disk-janitor.sh — keep the local-dev Docker/k3d disk footprint bounded.
#
# Tilt's built-in pruner can't be relied on for this: it has silent failure
# modes (a Docker hiccup at `tilt up` disables it for the whole session, and
# every per-image skip is Debug-level only), and it can never reach the k3d
# registry (tilt-dev/tilt#2102) or the k3s nodes' containerd stores
# (tilt-dev/tilt#4228). Left alone, those stores grow by several GB per app
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
#      by any pod, then garbage-collect blobs offline (the registry is
#      briefly stopped; deferred while a push is in flight).
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
# CreatedAt is formatted client-side in the local zone, where lexical sort
# breaks during the DST fall-back hour — TZ=UTC pins it to a sortable format
# ("2026-07-23 15:17:42 +0000 UTC").
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
        TZ=UTC docker images --filter reference='localhost:5001/*' \
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
# garbage collector (offline — see registry_gc).
# Delete candidates are snapshotted BEFORE the keep-set is computed, so a tag
# Tilt pushes mid-phase can never be a candidate (it would race the keep-set
# otherwise). If kubectl or docker fails we skip the whole phase — fail safe,
# prune nothing.
# Registry dirs are one level deep today: Tilt flattens image names
# (mitodl/mit-learn-app -> mitodl_mit-learn-app) when it prepends the
# registry. If a build ever pushes a nested repo (foo/bar), this ls walk
# silently misses it — failure direction is unbounded growth for that repo,
# never a wrong deletion.
# ---------------------------------------------------------------------------
REGISTRY_GC_PENDING=0

prune_registry() {
    container_running "$REGISTRY_CONTAINER" || return 0

    local candidates
    candidates="$(
        while IFS= read -r repo; do
            [[ -z "$repo" ]] && continue
            while IFS= read -r tag; do
                [[ -z "$tag" ]] && continue
                echo "${repo}:${tag}"
            done < <(docker exec "$REGISTRY_CONTAINER" \
                        ls "${REGISTRY_DATA}/repositories/${repo}/_manifests/tags" 2>/dev/null)
        done < <(docker exec "$REGISTRY_CONTAINER" \
                    ls "${REGISTRY_DATA}/repositories" 2>/dev/null)
    )"
    [[ -z "$candidates" ]] && return 0

    local pod_refs
    if ! pod_refs="$(pod_image_refs)"; then
        log "registry: kubectl unavailable — skipping registry retention this cycle"
        return 0
    fi
    local local_tags
    if ! local_tags="$(docker images --filter reference='localhost:5001/*' \
            --format '{{.Repository}}:{{.Tag}}' 2>/dev/null)"; then
        log "registry: docker images failed — skipping registry retention this cycle"
        return 0
    fi

    local keep
    keep="$(
        {
            echo "$local_tags"
            echo "$pod_refs"
        } | strip_registry_prefix | sort -u
    )"

    local removed=0 candidate repo tag
    while IFS= read -r candidate; do
        [[ -z "$candidate" ]] && continue
        if ! grep -qxF "$candidate" <<<"$keep"; then
            repo="${candidate%%:*}"
            tag="${candidate#*:}"
            if docker exec "$REGISTRY_CONTAINER" \
                rm -rf "${REGISTRY_DATA}/repositories/${repo}/_manifests/tags/${tag}"; then
                removed=$((removed + 1))
            fi
        fi
    done <<<"$candidates"

    if (( removed > 0 )); then
        log "registry: removed $removed stale tag(s)"
        REGISTRY_GC_PENDING=1
    fi
}

# Blob GC runs OFFLINE: stop the registry, collect in a throwaway container
# sharing its volume, start it again. Online GC is unsafe by construction —
# a concurrent push whose layers "already exist" writes nothing to _uploads
# (only HEAD checks + a manifest PUT), so no guard can see it, and the
# registry's in-memory blob-descriptor cache would answer those HEADs from
# blobs GC just deleted, storing a manifest with missing blobs (the
# 2026-07-23 incident). With the registry stopped, a racing push fails
# loudly and Docker/Tilt retries; and each start begins with a cold cache,
# so no restart choreography is needed. GC stays pending across cycles until
# it succeeds (a deferral is otherwise lost if no later cycle removes tags).
registry_gc() {
    container_running "$REGISTRY_CONTAINER" || return 0

    # A visibly in-flight upload means Tilt is mid-push; defer rather than
    # fail its push. (Politeness only — stopping the registry is what makes
    # GC safe. A stale interrupted upload can defer this repeatedly; the
    # registry purges those after ~168h.)
    if docker exec "$REGISTRY_CONTAINER" \
        sh -c "find ${REGISTRY_DATA}/repositories -path '*/_uploads/*' -type f 2>/dev/null | head -1" \
        | grep -q .; then
        log "registry: push in flight — deferring blob GC"
        return 0
    fi
    # --delete-untagged (registry >= 2.8) also drops the manifests our tag
    # deletion orphaned; without it blobs stay pinned and GC frees ~nothing.
    if ! docker exec "$REGISTRY_CONTAINER" registry garbage-collect --help 2>&1 \
        | grep -q -- --delete-untagged; then
        log "registry: 'registry garbage-collect --delete-untagged' unsupported — tags removed but blobs not freed"
        REGISTRY_GC_PENDING=0
        return 0
    fi

    local image rc=0
    image="$(docker inspect --format '{{.Config.Image}}' "$REGISTRY_CONTAINER" 2>/dev/null)"
    if [[ -z "$image" ]]; then
        log "registry: cannot determine registry image — deferring blob GC"
        return 0
    fi
    docker stop "$REGISTRY_CONTAINER" >/dev/null 2>&1
    docker run --rm --volumes-from "$REGISTRY_CONTAINER" "$image" \
        garbage-collect --delete-untagged /etc/docker/registry/config.yml \
        >/dev/null 2>&1 || rc=$?
    if ! docker start "$REGISTRY_CONTAINER" >/dev/null 2>&1; then
        log "registry: FAILED TO RESTART ${REGISTRY_CONTAINER} after GC — start it manually (docker start ${REGISTRY_CONTAINER})"
        return 0
    fi
    if (( rc == 0 )); then
        REGISTRY_GC_PENDING=0
        log "registry: offline blob GC complete"
    else
        log "registry: blob GC failed (will retry next cycle)"
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

    # --max-used-space (buildx >= 0.18, needs BuildKit >= 0.17) is the newer
    # total-size cap; older tooling spells it --keep-storage (since
    # deprecated in favor of --reserved-space). Both accept "<N>gb".
    local flag="--keep-storage"
    docker builder prune --help 2>&1 | grep -q -- --max-used-space && flag="--max-used-space"
    docker builder prune -f "$flag" "${cap}gb" >/dev/null 2>&1 \
        || log "build cache: prune failed"
}

run_cycle() {
    prune_local_tags
    prune_registry
    (( REGISTRY_GC_PENDING )) && registry_gc
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
