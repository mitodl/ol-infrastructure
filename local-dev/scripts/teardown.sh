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

# Is `stack` present in the current project's backend?
#   0 = yes, 1 = no, 2 = the listing could not be read.
#
# The third case has to stay distinct from the second. `pulumi stack ls | grep`
# reports the grep's status, so a backend that cannot be read looks exactly
# like a backend with nothing in it — and "nothing to destroy" is precisely the
# answer that lets teardown delete the cluster out from under live state.
stack_exists() {
    local stack="$1" listing rc=0

    listing="$(pulumi stack ls --json 2>/dev/null)" || return 2
    printf '%s' "${listing}" | python3 -c '
import json, sys
try:
    stacks = [s["name"] for s in json.load(sys.stdin)]
except Exception:
    sys.exit(2)
sys.exit(0 if sys.argv[1] in stacks else 1)
' "${stack}" || rc=$?
    return "${rc}"
}

# Throw away a stack's state. `pulumi stack rm` deletes Pulumi.<stack>.yaml
# along with it, and that config file is checked into this repo — save and
# restore it. Returns nonzero if the state is still there afterwards, so
# callers stop rather than proceeding to delete the cluster.
discard_stack_state() {
    local dir="$1" stack="$2" label="$3"
    local config="Pulumi.${stack}.yaml"
    local rc=0

    cd "${REPO_ROOT}/${dir}"
    [[ -f "${config}" ]] && cp "${config}" "${config}.teardown-bak"
    if PULUMI_CONFIG_PASSPHRASE='' pulumi stack rm "${stack}" --force --yes >/dev/null 2>&1; then
        ok "    ${label} state discarded (stack config preserved)."
    else
        warn "    Could not remove stack '${stack}'. Remove it by hand, then"
        warn "    re-run teardown:  cd ${dir} && pulumi stack rm ${stack} --force --yes"
        rc=1
    fi
    [[ -f "${config}.teardown-bak" ]] && mv "${config}.teardown-bak" "${config}"
    return "${rc}"
}

# Destroy one Pulumi stack, and make sure its state cannot outlive the
# cluster. Everything these stacks manage lives inside the cluster that gets
# deleted below, but the state lives in this checkout and survives — so a
# failed destroy leaves Pulumi believing resources exist that are already
# gone, and the next `pulumi up` skips creating them.
#
# That is not hypothetical: on 2026-08-13 a broken registry kept Keycloak
# from starting, the apps_infra destroy failed, `|| true` reported success
# anyway, and the first `pulumi up` against the rebuilt cluster died with
# 404 "Realm not found" while creating a child of a realm Pulumi thought it
# already had.
#
# Every failure path out of here is nonzero: with `set -e` that aborts
# teardown before `k3d cluster delete`, leaving the cluster and its state
# consistent with each other for a retry. A cluster you can delete by hand
# beats state Pulumi will silently trust on the next `pulumi up`.
destroy_stack() {
    local dir="$1" stack="$2" label="$3"
    local rc=0

    cd "${REPO_ROOT}/${dir}"
    stack_exists "${stack}" || rc=$?
    if (( rc == 2 )); then
        warn "    Could not read the Pulumi stack list in ${dir}."
        warn "    Not deleting the cluster: any state there would be stranded."
        warn "    Fix the backend and re-run teardown."
        return 1
    fi
    if (( rc == 1 )); then
        ok "    No ${label} state found."
        return 0
    fi

    if PULUMI_CONFIG_PASSPHRASE='' pulumi destroy --stack "${stack}" --yes --logtostderr; then
        ok "    ${label} destroyed."
        return 0
    fi

    warn "    'pulumi destroy' failed for ${label} — discarding its state."
    warn "    Keeping it would make the next 'pulumi up' skip resources it"
    warn "    wrongly believes still exist in the deleted cluster."
    discard_stack_state "${dir}" "${stack}" "${label}"
}

# Same guarantee from the other direction: the cluster is already gone (deleted
# by hand, or by a teardown that was interrupted after `k3d cluster delete`),
# so there is no API server left to destroy anything against and the state is
# stale by definition. Discard it, or setup.sh reuses it.
drop_orphan_stack() {
    local dir="$1" stack="$2" label="$3"
    local rc=0

    cd "${REPO_ROOT}/${dir}"
    stack_exists "${stack}" || rc=$?
    if (( rc == 2 )); then
        warn "    Could not read the Pulumi stack list in ${dir}."
        warn "    Fix the backend and re-run teardown — leftover state here"
        warn "    would break the next 'pulumi up'."
        return 1
    fi
    if (( rc == 1 )); then
        ok "    No ${label} state found."
        return 0
    fi

    warn "    ${label} state outlived its cluster — discarding it."
    discard_stack_state "${dir}" "${stack}" "${label}"
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
    destroy_stack "local-dev/infra/apps_infra" "local-dev.apps-infra.Dev" "Apps infrastructure"

    # Destroy core stack (after apps_infra is gone)
    log "  Destroying core stack..."
    destroy_stack "local-dev/infra/core" "local-dev.core.Dev" "Core infrastructure"

    cd "${REPO_ROOT}"

    # Now delete the cluster
    k3d cluster delete "${CLUSTER_NAME}"
    ok "Cluster deleted."
else
    warn "Cluster '${CLUSTER_NAME}' not found — nothing to delete."

    log "Checking for Pulumi state left behind by the missing cluster..."
    drop_orphan_stack "local-dev/infra/apps_infra" "local-dev.apps-infra.Dev" "Apps infrastructure"
    drop_orphan_stack "local-dev/infra/core" "local-dev.core.Dev" "Core infrastructure"
    cd "${REPO_ROOT}"
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
