#!/usr/bin/env bash
# disk-janitor.sh — keep the local-dev Docker disk footprint bounded.
#
# Tilt's built-in pruner can't be relied on for this: it has silent failure
# modes (a Docker hiccup at `tilt up` disables it for the whole session, and
# every per-image skip is Debug-level only). Left alone, tilt-built images
# and build cache grow by several GB per app rebuild.
#
# This janitor enforces a retention policy instead of reacting to low disk.
# Retention (keep the newest N tags) is safe to run at any moment — unlike a
# wipe, it can never delete an image something is about to need. It runs as
# a Tilt serve_cmd resource, so it is alive exactly when builds (the only
# source of growth) can happen.
#
#   1. Local Docker daemon: keep the newest N tilt-built tags per repo
#      (localhost:5001/*); remove older ones. Never touches other images.
#   2. Docker build cache: prune down to a size cap, least-recently-used
#      first. NOTE: the BuildKit cache is one daemon-wide pool shared with
#      everything else you build on this machine, so this is the one step
#      whose effect is not scoped to local-dev (the cost is only rebuild
#      speed, never correctness). Set the cap to 0 to opt out and manage the
#      pool yourself (e.g. daemon builder.gc config).
#
# The other two image stores clean themselves and are intentionally NOT
# handled here:
#   - k3d registry: zot enforces its own retention + GC declaratively
#     (local-dev/cluster/zot-config.json).
#   - k3s node containerd stores: kubelet's image GC owns those (thresholds
#     in local-dev/cluster/k3d-config.yaml).
#
# Knobs (wired from tilt_config.json / env by the root Tiltfile):
#   JANITOR_KEEP_TAGS          newest tags kept per repo (default 3)
#   JANITOR_BUILDCACHE_MAX_GB  build-cache cap in GB; empty = 10% of total
#                              disk; 0 = leave the build cache alone
#   JANITOR_INTERVAL_SECS      loop interval (default 1800); 0 = single pass
#
# Usage: ./local-dev/scripts/disk-janitor.sh   (or let Tilt run it)

set -uo pipefail

KEEP_TAGS="${JANITOR_KEEP_TAGS:-3}"
INTERVAL="${JANITOR_INTERVAL_SECS:-1800}"

ts() { date "+%Y-%m-%d %H:%M:%S"; }
log() { echo "[$(ts)] $*"; }

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

# ---------------------------------------------------------------------------
# 2. Build cache: cap the daemon-wide BuildKit pool, LRU eviction.
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

# ---------------------------------------------------------------------------
# Registry backend check. This janitor deliberately does NOT prune the
# registry — zot does that itself — so a machine still on the pre-2026-07
# registry:2 container has NO registry retention at all and will regrow
# unbounded. setup.sh prints migration instructions, but plenty of setups
# never re-run it; this warning runs where everyone actually is (tilt up).
# ---------------------------------------------------------------------------
warn_if_not_zot() {
    local image
    image="$(docker inspect k3d-registry.localhost --format '{{.Config.Image}}' 2>/dev/null)" || return 0
    case "$image" in *zot*) return 0 ;; esac
    log "WARNING: registry is '$image', not zot — it has no retention and will grow unbounded."
    log "WARNING: migrate (registry contents are disposable; Tilt re-pushes on next build):"
    log "WARNING:   k3d registry delete k3d-registry.localhost && ./local-dev/scripts/setup.sh"
}

run_cycle() {
    warn_if_not_zot
    prune_local_tags
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
