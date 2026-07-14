#!/usr/bin/env bash
# heal-exec.sh — repair wedged kubelet exec/streaming on the local-dev cluster.
#
# Why this exists: the API server proxies `kubectl exec` / `attach` / `logs -f`
# to each node's kubelet on :10250. After the Docker VM (OrbStack or Docker
# Desktop) is paused on Mac sleep and resumed, that streaming server can come
# back wedged on a node — the node
# still reports Ready and ordinary kubectl (get / describe / logs) keeps
# working, but exec fails with:
#
#     error: Internal error occurred: error sending request: Post
#     "https://<node-ip>:10250/exec/...": proxy error from 127.0.0.1:6443
#     while dialing <node-ip>:10250, code 502: 502 Bad Gateway
#
# Restarting Tilt only recycles workloads; it does not touch the node
# containers, so it cannot fix this. `docker restart`ing the affected node
# container rebuilds the kubelet and clears the wedge — and, unlike
# `k3d cluster stop/start`, it keeps the node's IP, so it does not trigger the
# node-IP reshuffle that breaks kubelet serving-cert SANs and node registration.
#
# This script probes each node's kubelet through the API server proxy (the same
# :10250 path exec rides) and `docker restart`s only the nodes that fail, then
# waits for them to recover. It is a no-op when everything is healthy, so it is
# safe to run on every session start and on wake.
#
# Usage: ./local-dev/scripts/heal-exec.sh

set -euo pipefail

CONTEXT="local-dev"
READY_TIMEOUT="120s"      # kubectl wait for a node to report Ready after restart
PROBE_RETRIES=8
PROBE_INTERVAL=3
DOCKER_READY_WAIT=90     # seconds to wait for the Docker engine to resume on wake
CLUSTER_READY_WAIT=90    # seconds to wait for the cluster API to resume on wake
RESTART_ATTEMPTS=3
REQUEST_TIMEOUT="10s"    # per-call kubectl timeout so an unreachable node fails fast

log()  { echo "▶ $*"; }
ok()   { echo "  ✓ $*"; }
warn() { echo "  ⚠ $*" >&2; }
err()  { echo "  ✗ $*" >&2; exit 1; }

# Is the node's kubelet reachable through the API server proxy? This is the
# same apiserver->kubelet :10250 path that exec/attach/logs-follow use, so a
# dial-level wedge fails this probe too.
probe() {
	kubectl --context "$CONTEXT" --request-timeout="$REQUEST_TIMEOUT" \
		get --raw "/api/v1/nodes/$1/proxy/healthz" >/dev/null 2>&1
}

# Run "$@" repeatedly until it succeeds or `seconds` elapse.
wait_ready() {
	local seconds="$1" i
	shift
	for ((i = 0; i < seconds; i++)); do
		"$@" >/dev/null 2>&1 && return 0
		sleep 1
	done
	return 1
}

# docker restart with retries; on failure surface docker's actual stderr (the
# original version swallowed it, so wake-time failures were undiagnosable).
restart_node() {
	local node="$1" attempt out
	for ((attempt = 1; attempt <= RESTART_ATTEMPTS; attempt++)); do
		if out=$(docker restart "$node" 2>&1); then
			return 0
		fi
		warn "docker restart $node failed (attempt $attempt/$RESTART_ATTEMPTS): $out"
		sleep 3
	done
	return 1
}

# Probe with a few retries — after a node container restart the kubelet takes a
# moment to rebind :10250.
probe_retry() {
	local node="$1" i
	for ((i = 1; i <= PROBE_RETRIES; i++)); do
		probe "$node" && return 0
		sleep "$PROBE_INTERVAL"
	done
	return 1
}

# Fail fast with a clear message if the tools are missing. In a launchd /
# sleepwatcher hook the PATH is minimal, and the docker CLI lives outside the
# Homebrew dirs (~/.orbstack/bin for OrbStack, ~/.docker/bin for Docker Desktop)
# — a missing binary would otherwise masquerade as the confusing 90s "not
# ready" timeout below.
for bin in docker kubectl; do
	command -v "$bin" >/dev/null 2>&1 || err "'$bin' not found on PATH ($PATH).
        If this ran from a wake hook, ensure it exports the docker CLI dir (~/.orbstack/bin or ~/.docker/bin) plus /opt/homebrew/bin or /usr/local/bin."
done

# On wake, the Docker VM (OrbStack or Docker Desktop) — and the Docker engine + k3s cluster inside it —
# resume asynchronously, and the sleepwatcher hook fires immediately. Wait,
# bounded, for both to come back before doing anything; otherwise our docker
# and kubectl calls race the resume and fail. When the cluster is simply
# stopped (not resuming), these time out and we skip — that is not ours to fix.
if ! wait_ready "$DOCKER_READY_WAIT" docker info; then
	warn "Docker engine not ready after ${DOCKER_READY_WAIT}s; skipping exec heal."
	exit 0
fi
if ! wait_ready "$CLUSTER_READY_WAIT" \
	kubectl --context "$CONTEXT" --request-timeout="$REQUEST_TIMEOUT" get nodes; then
	warn "Cluster '$CONTEXT' API not ready after ${CLUSTER_READY_WAIT}s; skipping exec heal."
	exit 0
fi

NODES=()
while IFS= read -r n; do
	[[ -n "$n" ]] && NODES+=("$n")
done < <(kubectl --context "$CONTEXT" get nodes \
	-o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}')

# Detect with probe_retry, not a single probe: a node that is merely slow to
# rebind :10250 right after wake would otherwise be misread as wedged and
# needlessly restarted (an expensive full node bounce). probe_retry returns on
# the first success, so a healthy node still costs just one probe.
wedged=()
for node in "${NODES[@]}"; do
	if probe_retry "$node"; then
		ok "exec streaming healthy: $node"
	else
		warn "exec streaming wedged: $node"
		wedged+=("$node")
	fi
done

if [[ ${#wedged[@]} -eq 0 ]]; then
	ok "All ${#NODES[@]} nodes healthy — nothing to heal."
	exit 0
fi

failed=()
for node in "${wedged[@]}"; do
	log "Restarting node container '$node' to clear the wedge..."
	if ! restart_node "$node"; then
		failed+=("$node")
		continue
	fi
	if ! kubectl --context "$CONTEXT" wait --for=condition=Ready "node/$node" \
		--timeout="$READY_TIMEOUT" >/dev/null 2>&1; then
		warn "Node '$node' not Ready within $READY_TIMEOUT after restart; re-probing anyway."
	fi
	if probe_retry "$node"; then
		ok "Healed: $node"
	else
		warn "Still wedged after restart: $node"
		failed+=("$node")
	fi
done

if [[ ${#failed[@]} -gt 0 ]]; then
	warn "Could not heal: ${failed[*]}"
	exit 1
fi

ok "Exec streaming restored on: ${wedged[*]}"
