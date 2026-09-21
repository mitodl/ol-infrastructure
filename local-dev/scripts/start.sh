#!/usr/bin/env bash
# start.sh — Start the MIT Learn local development environment.
#
# What this script does:
#   1. Validates that setup.sh has been run (cluster exists, kubeconfig configured)
#   2. Syncs Python dependencies via uv
#   3. Starts the Tilt development server
#
# Prerequisites: setup.sh must be run first.
#
# Usage:
#   ./local-dev/scripts/start.sh [tilt flags]
#   ./local-dev/scripts/start.sh --port 10351
#   ./local-dev/scripts/start.sh --host 0.0.0.0

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
CLUSTER_NAME="local-dev"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
log() { echo "▶ $*"; }
ok() { echo "  ✓ $*"; }
warn() { echo "  ⚠ $*"; }
err() {
	echo "  ✗ $*" >&2
	exit 1
}

# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------
log "Validating local dev environment..."

# Check that k3d cluster exists
if ! k3d cluster list 2>/dev/null | grep -q "^${CLUSTER_NAME}"; then
	err "Cluster '${CLUSTER_NAME}' not found. Run ./local-dev/scripts/setup.sh first."
fi
ok "Cluster '${CLUSTER_NAME}' found."

# Start the cluster if it is stopped (stop.sh pauses it via 'k3d cluster stop').
# The SERVERS column reports running/total (e.g. "1/1" running, "0/1" stopped).
SERVERS_STATUS="$(k3d cluster list "${CLUSTER_NAME}" --no-headers 2>/dev/null | awk '{print $2}')"
if [[ "${SERVERS_STATUS}" == 0/* ]]; then
	log "Cluster '${CLUSTER_NAME}' is stopped. Starting it..."
	if ! k3d cluster start "${CLUSTER_NAME}"; then
		err "Failed to start cluster '${CLUSTER_NAME}'. Try 'k3d cluster start ${CLUSTER_NAME}' manually."
	fi
	ok "Cluster '${CLUSTER_NAME}' started."
else
	ok "Cluster '${CLUSTER_NAME}' is running."
fi

# The registry is not a cluster node (no k3d.cluster label), so 'k3d cluster
# start' never touches it, and its unless-stopped policy will not revive it
# after an explicit stop (Docker Desktop dashboard, docker stop, a failed
# prune). Nothing else brings it back, and every image push then times out.
REGISTRY_STATE="$(docker inspect -f '{{.State.Running}}' k3d-registry.localhost 2>/dev/null)" ||
	err "Registry 'k3d-registry.localhost' not found. Run ./local-dev/scripts/setup.sh first."
if [[ "${REGISTRY_STATE}" != "true" ]]; then
	log "Registry 'k3d-registry.localhost' is stopped. Starting it..."
	docker start k3d-registry.localhost >/dev/null || err "Failed to start registry. Try 'docker start k3d-registry.localhost' manually."
fi
ok "Registry 'k3d-registry.localhost' is running."

# Check that kubeconfig context exists
if ! kubectl config get-contexts "local-dev" &>/dev/null; then
	err "kubectl context 'local-dev' not found. Run ./local-dev/scripts/setup.sh first."
fi
ok "kubectl context 'local-dev' configured."

# Set active context
kubectl config use-context "local-dev" &>/dev/null

# Check that TLS certs exist
CERT_DIR="${REPO_ROOT}/local-dev/certs"
CERT_FILE="${CERT_DIR}/local-dev.pem"
if [[ ! -f "$CERT_FILE" ]]; then
	err "TLS certificates not found at ${CERT_FILE}. Run ./local-dev/scripts/setup.sh first."
fi
ok "TLS certificates found."

# ---------------------------------------------------------------------------
# Is this the cluster the Pulumi state was written against?
# ---------------------------------------------------------------------------
# The stacks' state lives in this checkout and survives anything that happens
# to Docker, so a cluster replaced without teardown.sh — deleted by hand, swept
# up by `docker system prune`, lost to a WSL reset — leaves Pulumi describing a
# cluster that no longer exists. The stacks recover on their own (both run
# `pulumi refresh` before applying), but every database in the new cluster is
# empty, and without a word here that reads as data quietly disappearing.
#
# kube-system's UID is assigned when the cluster is bootstrapped, so it changes
# whenever the cluster does. It is stored beside the state it describes: wiping
# .pulumi takes the recorded id with it, which is right — state that no longer
# exists makes no claim about which cluster it belonged to.
CLUSTER_ID_FILE="${REPO_ROOT}/local-dev/infra/.pulumi/cluster-id"
live_cluster_id="$(kubectl get namespace kube-system -o jsonpath='{.metadata.uid}' 2>/dev/null)" || true

if [[ -z "${live_cluster_id}" ]]; then
	warn "Could not read the cluster's identity; skipping the replaced-cluster check."
elif [[ ! -f "${CLUSTER_ID_FILE}" ]]; then
	mkdir -p "$(dirname "${CLUSTER_ID_FILE}")"
	echo "${live_cluster_id}" >"${CLUSTER_ID_FILE}"
	ok "Recorded this cluster's identity."
elif [[ "$(cat "${CLUSTER_ID_FILE}")" != "${live_cluster_id}" ]]; then
	warn "This is not the cluster the Pulumi state was written against."
	warn "  It was replaced without teardown.sh — deleted by hand, or swept up by"
	warn "  'docker system prune', which removes stopped containers and then the"
	warn "  anonymous volumes holding every node's data."
	warn "  The infra stacks will reconcile and rebuild what is missing, but the"
	warn "  app databases in the new cluster are empty."
	# Newest dump that is actually complete. pg-backup.sh creates its directory
	# up front, so an interrupted run leaves one behind. Two checks, because
	# either alone lets a partial through: keycloak-users.json is the last
	# artifact the script writes, so it stands in for "the dump loop finished"
	# — the loop appends each manifest line only after mv-ing that database's
	# .dump into place, which keeps the counts consistent on a prefix — and the
	# manifest line count against the number of dumps is the same check
	# pg-restore.sh makes before it will load anything.
	# `|| true`: no .backups directory is the common case, and pipefail would
	# otherwise make find's exit status abort the script mid-warning.
	newest_backup=""
	while read -r candidate; do
		[[ -n "${candidate}" && -f "${candidate}/manifest.txt" ]] || continue
		[[ -s "${candidate}/keycloak-users.json" ]] || continue
		listed="$(wc -l <"${candidate}/manifest.txt" | tr -d ' ')"
		dumps="$(find "${candidate}" -maxdepth 1 -name '*.dump' | wc -l | tr -d ' ')"
		if [[ "${listed}" == "${dumps}" && "${dumps}" != "0" ]]; then
			newest_backup="${candidate}"
			break
		fi
	done < <(find "${REPO_ROOT}/local-dev/.backups" -mindepth 1 -maxdepth 1 -type d 2>/dev/null | sort -r || true)
	if [[ -n "${newest_backup}" ]]; then
		warn "  Newest dump is ${newest_backup##*/} — load it with:"
		warn "    ./local-dev/scripts/pg-restore.sh local-dev/.backups/${newest_backup##*/}"
	else
		warn "  No complete dump to restore from: pg-backup.sh only runs when you"
		warn "  run it, and a prune is never 'before' anything. Worth running"
		warn "  ./local-dev/scripts/pg-backup.sh once there is data worth keeping."
	fi
	echo "${live_cluster_id}" >"${CLUSTER_ID_FILE}"
else
	ok "Pulumi state matches this cluster."
fi

# ---------------------------------------------------------------------------
# Heal wedged kubelet exec/streaming (post-sleep recovery)
# ---------------------------------------------------------------------------
# After a Docker VM pause on Mac sleep (OrbStack or Docker Desktop), a node's kubelet exec/streaming server
# can come back wedged, so `kubectl exec` 502s even though the node is Ready.
# This is a no-op when everything is healthy. Best-effort: never block startup.
log "Checking kubelet exec/streaming health..."
if ! "${SCRIPT_DIR}/heal-exec.sh"; then
	warn "Exec heal reported problems; continuing to start Tilt anyway."
fi

# ---------------------------------------------------------------------------
# Sync Python dependencies
# ---------------------------------------------------------------------------
log "Syncing Python dependencies via uv..."

if ! command -v uv &>/dev/null; then
	err "uv not found. Install it and re-run: https://docs.astral.sh/uv/getting-started/installation/"
fi

cd "${REPO_ROOT}"
uv sync --quiet
ok "Python dependencies synced."

# ---------------------------------------------------------------------------
# Start Tilt
# ---------------------------------------------------------------------------
log "Starting Tilt..."
log "  Tilt UI will be available at http://localhost:10350"
log "  Press Ctrl+C to stop Tilt."
log "  To pause the cluster for a fast resume later, run:"
log "    ./local-dev/scripts/stop.sh"
log "  To destroy the entire cluster, run:"
log "    ./local-dev/scripts/teardown.sh"
log ""

# Pass all arguments to tilt up (allows --port, --host, etc.)
exec tilt up "$@"
