#!/usr/bin/env bash
# teardown.sh — Destroy the MIT Learn local development environment.
#
# Usage:
#   ./local-dev/scripts/teardown.sh [--keep-certs] [--keep-hosts]

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
CLUSTER_NAME="mit-learn-dev"

KEEP_CERTS=false
KEEP_HOSTS=false

while [[ $# -gt 0 ]]; do
    case "$1" in
        --keep-certs)  KEEP_CERTS=true;  shift ;;
        --keep-hosts)  KEEP_HOSTS=true;  shift ;;
        -h|--help)
            echo "Usage: teardown.sh [--keep-certs] [--keep-hosts]"
            exit 0
            ;;
        *) echo "Unknown argument: $1" >&2; exit 1 ;;
    esac
done

log()  { echo "▶ $*"; }
ok()   { echo "  ✓ $*"; }
warn() { echo "  ⚠ $*"; }

is_wsl() {
    [[ -n "${WSL_DISTRO_NAME:-}" ]] || grep -qi microsoft /proc/version 2>/dev/null
}

_remove_windows_hosts() {
    local win_hosts
    win_hosts=$(wslpath -u 'C:\Windows\System32\drivers\etc\hosts' 2>/dev/null) \
        || win_hosts="/mnt/c/Windows/System32/drivers/etc/hosts"

    if [[ ! -f "$win_hosts" ]]; then
        return
    fi

    if ! grep -q "# BEGIN mit-learn-dev local-dev" "$win_hosts" 2>/dev/null; then
        ok "No Windows hosts entries to remove."
        return
    fi

    if python3 -c "
import re
with open('${win_hosts}', 'r') as f:
    content = f.read()
content = re.sub(
    r'# BEGIN mit-learn-dev local-dev.*?# END mit-learn-dev local-dev\n?',
    '',
    content,
    flags=re.DOTALL,
)
with open('${win_hosts}', 'w') as f:
    f.write(content)
" 2>/dev/null; then
        ok "Windows hosts entries removed (${win_hosts})."
    else
        warn "Could not remove Windows hosts entries (requires Windows admin rights)."
        warn "Remove the '# BEGIN mit-learn-dev local-dev' block manually from"
        warn "C:\\Windows\\System32\\drivers\\etc\\hosts"
    fi
}

log "Destroying k3d cluster '${CLUSTER_NAME}'..."
if k3d cluster list 2>/dev/null | grep -q "^${CLUSTER_NAME}"; then
    k3d cluster delete "${CLUSTER_NAME}"
    ok "Cluster deleted."
else
    warn "Cluster '${CLUSTER_NAME}' not found — skipping."
fi

# ---------------------------------------------------------------------------
# Remove /etc/hosts entries
# ---------------------------------------------------------------------------
if ! $KEEP_HOSTS; then
    log "Removing /etc/hosts entries..."
    BLOCK_START="# BEGIN mit-learn-dev local-dev"
    if grep -q "${BLOCK_START}" /etc/hosts; then
        sudo python3 -c "
import re
with open('/etc/hosts', 'r') as f:
    content = f.read()
content = re.sub(
    r'# BEGIN mit-learn-dev local-dev.*?# END mit-learn-dev local-dev\n?',
    '',
    content,
    flags=re.DOTALL,
)
with open('/etc/hosts', 'w') as f:
    f.write(content)
"
        ok "/etc/hosts entries removed."
    else
        warn "No /etc/hosts block found — nothing to remove."
    fi

    if is_wsl; then
        log "WSL detected: removing Windows hosts entries..."
        _remove_windows_hosts
    fi
fi

# ---------------------------------------------------------------------------
# Remove TLS certificates
# ---------------------------------------------------------------------------
if ! $KEEP_CERTS; then
    CERT_DIR="${REPO_ROOT}/local-dev/certs"
    if [[ -d "${CERT_DIR}" ]]; then
        log "Removing certificates..."
        rm -f "${CERT_DIR}"/*.pem
        ok "Certificates removed."
    fi
fi

# ---------------------------------------------------------------------------
# Destroy Pulumi state and k3d cluster
# ---------------------------------------------------------------------------

log "Destroying Pulumi-managed resources..."
cd "${REPO_ROOT}/local-dev/infra"

# Destroy Pulumi state (if it exists)
if pulumi stack ls 2>/dev/null | grep -q "local-dev"; then
    pulumi destroy --stack local-dev.infra.Dev --yes --logtostderr || true
    ok "Pulumi resources destroyed."
else
    ok "No Pulumi state found (already cleaned up)."
fi

cd "${REPO_ROOT}"

# Delete k3d cluster
if k3d cluster list | grep -q "${CLUSTER_NAME}"; then
    log "Deleting k3d cluster '${CLUSTER_NAME}'..."
    k3d cluster delete "${CLUSTER_NAME}"
    ok "k3d cluster deleted."
else
    warn "k3d cluster '${CLUSTER_NAME}' not found — nothing to delete."
fi

echo ""
echo "Teardown complete. Run ./local-dev/scripts/setup.sh to start fresh."

