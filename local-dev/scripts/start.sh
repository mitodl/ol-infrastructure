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
