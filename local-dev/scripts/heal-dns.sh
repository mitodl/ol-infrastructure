#!/usr/bin/env bash
# heal-dns.sh — restart local-dev containers whose baked-in DNS went stale.
#
# Why this exists: Docker writes a container's /etc/resolv.conf when the
# container *starts*, from the host's resolver config at that moment, and never
# revisits it. Move the host between networks — home wifi to the office, VPN up
# or down — and every long-running container keeps forwarding DNS to resolvers
# that are no longer reachable. The containers stay up and look healthy; only
# name resolution is dead. The symptom is image pulls and image builds timing
# out on the registry:
#
#     failed to resolve reference "docker.io/library/python:3.12-slim-trixie":
#     dial tcp: lookup registry-1.docker.io on 10.87.0.1:53:
#     read udp ...->10.87.0.1:53: i/o timeout
#
# which surfaces in Tilt as ImagePullBackOff across the cluster and as dagger
# builds failing at `Container.from`. Nothing in the repo is broken, so it
# reads as a mystifying build failure.
#
# `docker restart` re-bakes resolv.conf from the host's current config, which
# is the whole fix. As in heal-exec.sh, that is deliberately preferred over
# `k3d cluster stop/start`: restarting the node container keeps the node's IP,
# so it does not trigger the node-IP reshuffle that breaks kubelet
# serving-cert SANs and node registration.
#
# This script compares each container's baked resolvers against the host's
# current ones and restarts only the containers that have actually drifted. It
# is a no-op when everything is current, so it is safe to run on every session
# start and on wake.
#
# Usage: ./local-dev/scripts/heal-dns.sh

set -euo pipefail

CONTEXT="local-dev"
CLUSTER_NAME="local-dev"
READY_TIMEOUT="120s"  # kubectl wait for a node to report Ready after restart
DOCKER_READY_WAIT=90  # seconds to wait for the Docker engine to resume on wake
CLUSTER_READY_WAIT=90 # seconds to wait for the cluster API to resume on wake
RESTART_ATTEMPTS=3
REQUEST_TIMEOUT="10s" # per-call kubectl timeout so an unreachable node fails fast
PROBE_NAME="registry-1.docker.io"
PROBE_TIMEOUT=2 # seconds per dig attempt against a suspect resolver
# Overridable so the detection logic can be exercised against a fixture file
# without touching the real one.
HOST_RESOLV="${HOST_RESOLV:-/etc/resolv.conf}"

log() { echo "▶ $*"; }
ok() { echo "  ✓ $*"; }
warn() { echo "  ⚠ $*" >&2; }
err() {
	echo "  ✗ $*" >&2
	exit 1
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

# dig is only used to confirm a suspected-stale resolver really is unreachable
# before paying for a restart. Without it we still detect drift, we just cannot
# second-guess it — which is the safe direction to fail, so degrade rather than
# abort.
HAVE_DIG=true
if ! command -v dig >/dev/null 2>&1; then
	HAVE_DIG=false
	warn "'dig' not found — skipping reachability confirmation; drift alone will trigger a restart."
fi

# ---------------------------------------------------------------------------
# Host resolver config — the reference every container is compared against
# ---------------------------------------------------------------------------

# Loopback resolvers (systemd-resolved's 127.0.0.53 stub, dnsmasq on 127.0.0.1)
# are meaningless inside a container: Docker drops them and substitutes its own
# public defaults. Comparing a container against a loopback-only host list
# would therefore read Docker's defaults as drift and restart everything, every
# run. Filter them out, and bail below if nothing survives.
host_nameservers() {
	awk '/^nameserver/ {print $2}' "$HOST_RESOLV" |
		grep -vE '^(127\.|::1$)' || true
}

host_search() {
	awk '/^search/ {$1=""; print substr($0,2); exit}' "$HOST_RESOLV"
}

HOST_NS="$(host_nameservers)"
HOST_SEARCH="$(host_search)"

if [[ -z "$HOST_NS" ]]; then
	warn "Host has no non-loopback nameservers in ${HOST_RESOLV}; cannot judge container DNS. Skipping."
	exit 0
fi

# ---------------------------------------------------------------------------
# Per-container inspection
# ---------------------------------------------------------------------------

container_resolv() {
	docker exec "$1" cat /etc/resolv.conf 2>/dev/null || true
}

# k3d nodes run behind Docker's internal resolver, which records the real
# upstreams in a trailing comment:
#   # ExtServers: [host(75.75.75.75) host(8.8.8.8) ...]
# That comment is the only place the upstreams are visible, since the
# nameserver line just points at Docker's embedded resolver.
baked_upstreams() {
	grep -o 'host([^)]*)' <<<"$1" | sed 's/^host(//; s/)$//' || true
}

baked_search() {
	awk '/^search/ {$1=""; print substr($0,2); exit}' <<<"$1"
}

# Does this resolver actually answer? Used to confirm that a resolver missing
# from the host's list is genuinely unreachable, rather than merely unfamiliar.
resolver_answers() {
	dig "@$1" +time="$PROBE_TIMEOUT" +tries=1 +short "$PROBE_NAME" >/dev/null 2>&1
}

# Docker substitutes its own public defaults when the host offers it no usable
# (non-loopback) nameserver, so these can appear in a container's upstream list
# without ever having been a host resolver. They are never the stale
# previous-network upstream this script hunts: they answer from any network with
# egress, so a container still pointing at them is not the breakage we are here
# to fix. Counting them as drift would restart healthy containers — and since a
# restart re-bakes the very same defaults, the confirmation below would then
# fail every run. Exclude them from the candidate set.
DOCKER_FALLBACK_NS=$'8.8.8.8\n8.8.4.4\n2001:4860:4860::8888\n2001:4860:4860::8844'

# A container is stale when it is still pointing at a resolver that the host has
# since dropped AND that no longer answers.
#
# Note the shape of this test. The obvious rule — "stale if none of its
# resolvers answer" — is wrong here, because Docker appends public fallbacks
# (8.8.8.8, 8.8.4.4) that answer from any network. The failure is not that
# every resolver is dead; it is that the *dead ones come first* and burn the
# client's entire timeout budget before the working fallback is ever reached.
# dagger logs the resolve being abandoned at ~20s; containerd reports the same
# failure as a bare `Try again` (EAI_AGAIN). So one unreachable stale entry is
# enough to break image pulls, even with a working resolver behind it.
upstreams_stale() {
	local conf="$1" ns found_stale=false
	local -a upstreams
	mapfile -t upstreams < <(baked_upstreams "$conf")
	[[ ${#upstreams[@]} -eq 0 ]] && return 1

	for ns in "${upstreams[@]}"; do
		grep -qxF "$ns" <<<"$HOST_NS" && continue
		grep -qxF "$ns" <<<"$DOCKER_FALLBACK_NS" && continue
		# Present in the container, absent from the host: a drift candidate.
		if ! $HAVE_DIG || ! resolver_answers "$ns"; then
			found_stale=true
		fi
	done
	$found_stale
}

# The dagger engine does not expose its upstreams at all: it replaces the
# nameserver lines with its own in-netns resolver and keeps only the search
# domain, leaving the real upstreams unreadable from outside:
#
#     # container ns resolver
#     nameserver 10.87.0.1
#     search hsd1.ma.comcast.net
#     # Based on host file: '/etc/resolv.conf' (legacy)
#
# The search domain is the only usable signal, so fall back to it. It is a
# weaker test — two networks that both lack a search domain are
# indistinguishable — but a dagger restart costs seconds and its build cache
# lives in a named volume that survives, so an occasional needless restart is
# cheap.
#
# "No search domain" is itself a configuration, not a missing reading: moving
# between a network that sets one and a network that does not is exactly the
# host network change this script exists to catch, and skipping that comparison
# would leave the dagger engine — whose upstreams are invisible, so this is the
# only test it gets — pointing at the previous network's resolvers. So compare
# empty against non-empty too. The one case with genuinely nothing to compare is
# an unreadable container config, which callers already screen for; guard it
# here as well so this is never read as "current" by mistake.
search_stale() {
	local conf="$1" their_search
	[[ -z "$conf" ]] && return 1
	their_search="$(baked_search "$conf")"
	[[ "$their_search" != "$HOST_SEARCH" ]]
}

# ---------------------------------------------------------------------------
# Restart helpers
# ---------------------------------------------------------------------------

# Each container's resolv.conf as it looked when we judged it stale, so the
# confirmation below can tell "the restart changed nothing" apart from "the
# restart did not fix it".
declare -A PRE_RESTART_CONF

# Confirm a restart actually achieved what it was for. Returns non-zero only
# when the container still needs healing that we failed to deliver.
#
# An unreadable resolv.conf must not read as success: every staleness test
# returns false on empty input, so skipping this check would report "Healed"
# for a container we never managed to inspect.
#
# A container that comes back byte-identical is a third outcome, distinct from
# both. The restart re-baked resolv.conf from the host's config as it stands
# now, so an unchanged result means this *is* what the host currently produces
# — our detector objected to a permanent feature of this setup, not to a
# leftover from the previous network. No restart can change that, so failing
# the run on it would just turn a false positive into a recurring error.
confirm_restart() {
	local name="$1" check_search="$2" before after
	# Separate statements: `local` expands every argument before it assigns any
	# of them, so a `before=${PRE_RESTART_CONF[$name]}` on the line above would
	# subscript with an as-yet-unset `name` and always read empty.
	before="${PRE_RESTART_CONF[$name]-}"
	after="$(container_resolv "$name")"
	if [[ -z "$after" ]]; then
		warn "Could not re-read /etc/resolv.conf in '$name' after restart; cannot confirm."
		return 1
	fi
	if ! upstreams_stale "$after" && { [[ "$check_search" != true ]] || ! search_stale "$after"; }; then
		ok "Healed: $name"
		return 0
	fi
	if [[ -n "$before" && "$after" == "$before" ]]; then
		warn "'$name' came back with an unchanged resolv.conf, so it already matches what this host produces now; treating the earlier stale reading as a false positive."
		return 0
	fi
	warn "Still stale after restart: $name"
	return 1
}

# docker restart with retries; surface docker's actual stderr on failure so a
# wake-time failure is diagnosable instead of silent.
restart_container() {
	local name="$1" attempt out
	for ((attempt = 1; attempt <= RESTART_ATTEMPTS; attempt++)); do
		if out=$(docker restart "$name" 2>&1); then
			return 0
		fi
		warn "docker restart $name failed (attempt $attempt/$RESTART_ATTEMPTS): $out"
		sleep 3
	done
	return 1
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

# ---------------------------------------------------------------------------
# Wait out a VM resume before touching anything
# ---------------------------------------------------------------------------
# On wake, the Docker VM (OrbStack or Docker Desktop) — and the Docker engine +
# k3s cluster inside it — resume asynchronously, and the sleepwatcher hook fires
# immediately. Wait, bounded, for both to come back; otherwise our docker and
# kubectl calls race the resume and fail. When the cluster is simply stopped
# (not resuming), these time out and we skip — that is not ours to fix.
if ! wait_ready "$DOCKER_READY_WAIT" docker info; then
	warn "Docker engine not ready after ${DOCKER_READY_WAIT}s; skipping DNS heal."
	exit 0
fi

# ---------------------------------------------------------------------------
# Collect candidates
# ---------------------------------------------------------------------------
# k3d labels every node container, including the serverlb, with the cluster
# name — more reliable than matching on the k3d-<cluster>- name prefix.
#
# The zot registry (k3d.role=registry) is deliberately NOT included: it only
# serves images pushed to it from this machine and never resolves an external
# name, so restarting it on a network change would be pure churn.
NODES=()
while IFS= read -r n; do
	[[ -n "$n" ]] && NODES+=("$n")
done < <(docker ps --filter "label=k3d.cluster=${CLUSTER_NAME}" --format '{{.Names}}')

DAGGER=()
while IFS= read -r n; do
	[[ -n "$n" ]] && DAGGER+=("$n")
done < <(docker ps --filter 'name=dagger-engine' --format '{{.Names}}')

if [[ ${#NODES[@]} -eq 0 && ${#DAGGER[@]} -eq 0 ]]; then
	warn "No local-dev node or dagger containers running; nothing to check."
	exit 0
fi

# ---------------------------------------------------------------------------
# Detect
# ---------------------------------------------------------------------------
log "Comparing container DNS against the host's current resolvers..."

stale_nodes=()
for node in "${NODES[@]:-}"; do
	[[ -z "$node" ]] && continue
	conf="$(container_resolv "$node")"
	if [[ -z "$conf" ]]; then
		warn "Could not read /etc/resolv.conf in '$node'; skipping."
		continue
	fi
	if upstreams_stale "$conf"; then
		warn "stale DNS: $node"
		stale_nodes+=("$node")
		PRE_RESTART_CONF["$node"]="$conf"
	else
		ok "DNS current: $node"
	fi
done

stale_dagger=()
for eng in "${DAGGER[@]:-}"; do
	[[ -z "$eng" ]] && continue
	conf="$(container_resolv "$eng")"
	if [[ -z "$conf" ]]; then
		warn "Could not read /etc/resolv.conf in '$eng'; skipping."
		continue
	fi
	# Prefer the upstream comparison when dagger happens to expose one; fall
	# back to the search domain, which is all it normally leaves visible.
	if upstreams_stale "$conf" || search_stale "$conf"; then
		warn "stale DNS: $eng"
		stale_dagger+=("$eng")
		PRE_RESTART_CONF["$eng"]="$conf"
	else
		ok "DNS current: $eng"
	fi
done

if [[ ${#stale_nodes[@]} -eq 0 && ${#stale_dagger[@]} -eq 0 ]]; then
	ok "All containers resolve via the host's current DNS — nothing to heal."
	exit 0
fi

# ---------------------------------------------------------------------------
# Heal
# ---------------------------------------------------------------------------
failed=()

# Restart agents and the loadbalancer before the control plane, so the API
# server stays up while the rest bounce and `kubectl wait` below has something
# to talk to. The server node goes last.
ordered_nodes=()
for node in "${stale_nodes[@]:-}"; do
	[[ -z "$node" ]] && continue
	[[ "$(docker inspect -f '{{index .Config.Labels "k3d.role"}}' "$node" 2>/dev/null)" == "server" ]] ||
		ordered_nodes+=("$node")
done
for node in "${stale_nodes[@]:-}"; do
	[[ -z "$node" ]] && continue
	[[ "$(docker inspect -f '{{index .Config.Labels "k3d.role"}}' "$node" 2>/dev/null)" == "server" ]] &&
		ordered_nodes+=("$node")
done

for node in "${ordered_nodes[@]:-}"; do
	[[ -z "$node" ]] && continue
	log "Restarting '$node' to re-bake its DNS..."
	if ! restart_container "$node"; then
		failed+=("$node")
		continue
	fi
	# The loadbalancer is not a Kubernetes node, so it never appears in
	# `kubectl get nodes` — only wait on real nodes.
	if [[ "$(docker inspect -f '{{index .Config.Labels "k3d.role"}}' "$node" 2>/dev/null)" != "loadbalancer" ]]; then
		if ! wait_ready "$CLUSTER_READY_WAIT" \
			kubectl --context "$CONTEXT" --request-timeout="$REQUEST_TIMEOUT" get nodes; then
			warn "Cluster '$CONTEXT' API did not come back within ${CLUSTER_READY_WAIT}s after restarting '$node'."
		elif ! kubectl --context "$CONTEXT" wait --for=condition=Ready "node/$node" \
			--timeout="$READY_TIMEOUT" >/dev/null 2>&1; then
			warn "Node '$node' not Ready within $READY_TIMEOUT after restart; continuing."
		fi
	fi
	confirm_restart "$node" false || failed+=("$node")
done

for eng in "${stale_dagger[@]:-}"; do
	[[ -z "$eng" ]] && continue
	log "Restarting '$eng' to re-bake its DNS..."
	if ! restart_container "$eng"; then
		failed+=("$eng")
		continue
	fi
	confirm_restart "$eng" true || failed+=("$eng")
done

if [[ ${#failed[@]} -gt 0 ]]; then
	warn "Could not heal: ${failed[*]}"
	exit 1
fi

healed=()
[[ ${#stale_nodes[@]} -gt 0 ]] && healed+=("${stale_nodes[@]}")
[[ ${#stale_dagger[@]} -gt 0 ]] && healed+=("${stale_dagger[@]}")
ok "DNS re-baked on: ${healed[*]}"

# In-flight image pulls that already failed stay in ImagePullBackOff until
# kubelet's backoff timer expires (up to ~5m). Mention it so a still-red Tilt
# right after a heal does not read as the heal having failed.
log "Note: pods already in ImagePullBackOff retry on kubelet's backoff timer (up to ~5m)."
log "      To retry immediately:  kubectl --context ${CONTEXT} delete pod -n <ns> <pod>"
