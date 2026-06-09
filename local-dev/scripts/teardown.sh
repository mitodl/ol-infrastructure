#!/usr/bin/env bash
# teardown.sh — Destroy the MIT Learn local development environment.
#
# Usage:
#   ./local-dev/scripts/teardown.sh [--keep-certs] [--keep-hosts]

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
CLUSTER_NAME="local-dev"

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

    if ! grep -q "# BEGIN local-dev local-dev" "$win_hosts" 2>/dev/null; then
        ok "No Windows hosts entries to remove."
        return
    fi

    if python3 -c "
import re
with open('${win_hosts}', 'r') as f:
    content = f.read()
content = re.sub(
    r'# BEGIN local-dev local-dev.*?# END local-dev local-dev\n?',
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
        warn "Remove the '# BEGIN local-dev local-dev' block manually from"
        warn "C:\\Windows\\System32\\drivers\\etc\\hosts"
    fi
}

log "Destroying k3d cluster '${CLUSTER_NAME}'..."
if k3d cluster list 2>/dev/null | grep -q "^${CLUSTER_NAME}"; then
    # Ensure cluster is running before destroying Pulumi resources
    log "  Ensuring cluster is running..."
    if ! k3d cluster list 2>/dev/null | grep -q "^${CLUSTER_NAME}.*1/1"; then
        log "  Starting cluster for resource cleanup..."
        k3d cluster start "${CLUSTER_NAME}"
        sleep 10  # Give cluster time to stabilize
        ok "Cluster started."
    else
        ok "Cluster is already running."
    fi

    # Now destroy Pulumi resources while cluster is running
    log "Destroying Pulumi-managed resources before cluster teardown..."

    # Destroy apps_infra stack first (it depends on core stack)
    log "  Destroying apps_infra stack..."
    cd "${REPO_ROOT}/local-dev/infra/apps_infra"
    if pulumi stack ls 2>/dev/null | grep -q "local-dev.apps-infra"; then
        PULUMI_CONFIG_PASSPHRASE='' pulumi destroy --stack local-dev.apps-infra.Dev --yes --logtostderr || true
        ok "    Apps infrastructure destroyed."
    else
        ok "    No apps_infra state found."
    fi

    # Destroy core stack (after apps_infra is gone)
    log "  Destroying core stack..."
    cd "${REPO_ROOT}/local-dev/infra/core"
    if pulumi stack ls 2>/dev/null | grep -q "local-dev.core"; then
        PULUMI_CONFIG_PASSPHRASE='' pulumi destroy --stack local-dev.core.Dev --yes --logtostderr || true
        ok "    Core infrastructure destroyed."
    else
        ok "    No core state found."
    fi

    cd "${REPO_ROOT}"

    # Now delete the cluster
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
    BLOCK_START="# BEGIN local-dev local-dev"
    if grep -q "${BLOCK_START}" /etc/hosts; then
        sudo python3 -c "
import re
with open('/etc/hosts', 'r') as f:
    content = f.read()
content = re.sub(
    r'# BEGIN local-dev local-dev.*?# END local-dev local-dev\n?',
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



echo ""
echo "Teardown complete. Run ./local-dev/scripts/setup.sh to start fresh."
