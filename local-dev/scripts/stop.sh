#!/usr/bin/env bash
# stop.sh — Stop the MIT Learn local development environment for easy resumption.
#
# What this script does:
#   1. Stops the Tilt development server
#   2. Pauses the k3d cluster (keeps resources intact for fast resume)
#   3. Preserves all Pulumi state and cluster resources
#
# To resume development later, run:
#   ./local-dev/scripts/start.sh
#
# To completely destroy the environment, run:
#   ./local-dev/scripts/teardown.sh
#
# Usage:
#   ./local-dev/scripts/stop.sh
#   ./local-dev/scripts/stop.sh --keep-running  # Keep cluster running (just stop Tilt)

set -euo pipefail

CLUSTER_NAME="local-dev"
KEEP_RUNNING=false

# Parse arguments
while [[ $# -gt 0 ]]; do
    case "$1" in
        --keep-running)
            KEEP_RUNNING=true
            shift
            ;;
        -h|--help)
            echo "Usage: stop.sh [--keep-running]"
            echo ""
            echo "Options:"
            echo "  --keep-running    Stop Tilt but leave the k3d cluster running"
            echo "  -h, --help        Show this help message"
            exit 0
            ;;
        *)
            echo "Unknown argument: $1" >&2
            exit 1
            ;;
    esac
done

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
log()  { echo "▶ $*"; }
ok()   { echo "  ✓ $*"; }
warn() { echo "  ⚠ $*"; }

# ---------------------------------------------------------------------------
# Stop Tilt
# ---------------------------------------------------------------------------
log "Stopping Tilt development server..."

# Try graceful shutdown via tilt (preferred)
if command -v tilt &>/dev/null; then
    if tilt down 2>/dev/null; then
        ok "Tilt stopped gracefully."
    else
        warn "Tilt graceful shutdown failed, trying force stop..."
        # Force kill tilt processes
        if pkill -f "tilt serve" 2>/dev/null || pkill -f "tilt up" 2>/dev/null; then
            sleep 2  # Give processes time to die
            ok "Tilt processes stopped."
        else
            ok "No Tilt processes found running."
        fi
    fi
else
    warn "tilt command not found, skipping tilt shutdown."
fi

# ---------------------------------------------------------------------------
# Pause k3d cluster (optional)
# ---------------------------------------------------------------------------
if [[ "$KEEP_RUNNING" == "true" ]]; then
    ok "Keeping k3d cluster running (--keep-running specified)."
    exit 0
fi

log "Pausing k3d cluster..."

# Check that cluster exists
if ! k3d cluster list 2>/dev/null | grep -q "^${CLUSTER_NAME}"; then
    warn "Cluster '${CLUSTER_NAME}' not found — nothing to pause."
    exit 0
fi

# Check if cluster is running
if k3d cluster list 2>/dev/null | grep -q "^${CLUSTER_NAME}.*1/1"; then
    log "  Pausing cluster (may take 10-30 seconds)..."
    if k3d cluster stop "${CLUSTER_NAME}"; then
        ok "Cluster paused."
        echo ""
        ok "To resume, run: ./local-dev/scripts/start.sh"
    else
        warn "Failed to pause cluster. Try 'k3d cluster stop ${CLUSTER_NAME}' manually."
        exit 1
    fi
else
    ok "Cluster is already stopped or paused."
fi

echo ""
log "Stop complete!"
echo "  • All resources preserved"
echo "  • To resume: ./local-dev/scripts/start.sh"
echo "  • To destroy: ./local-dev/scripts/teardown.sh"
