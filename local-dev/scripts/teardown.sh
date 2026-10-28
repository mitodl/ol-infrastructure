#!/usr/bin/env bash
# teardown.sh — Destroy the MIT Learn local development environment.
#
# Usage:
#   ./local-dev/scripts/teardown.sh [--keep-certs] [--keep-hosts] [--drop-images]
#
# Node images are kept for the next setup.sh unless --drop-images is given;
# see keep_node_images below.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
CLUSTER_NAME="local-dev"

KEEP_CERTS=false
KEEP_HOSTS=false
DROP_IMAGES=false

while [[ $# -gt 0 ]]; do
    case "$1" in
        --keep-certs)  KEEP_CERTS=true;  shift ;;
        --keep-hosts)  KEEP_HOSTS=true;  shift ;;
        --drop-images) DROP_IMAGES=true; shift ;;
        -h|--help)
            echo "Usage: teardown.sh [--keep-certs] [--keep-hosts] [--drop-images]"
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

CONTAINERD_DIR="/var/lib/rancher/k3s/agent/containerd"

# The volume that keeps a node's image store: k3d-local-dev-agent-1 →
# local-dev-containerd-agent-1. Must match `volumes:` in k3d-config.yaml.
node_image_volume() {
    echo "${CLUSTER_NAME}-containerd-${1#"k3d-${CLUSTER_NAME}-"}"
}

# Name of the volume mounted at `dest` in container `node`, empty if none.
mounted_volume() {
    local node="$1" dest="$2"
    docker inspect "${node}" --format \
        "{{range .Mounts}}{{if eq .Destination \"${dest}\"}}{{.Name}}{{end}}{{end}}"
}

kept_image_volumes() {
    docker volume ls -q | grep "^${CLUSTER_NAME}-containerd-" || true
}

# Stop the cluster so every node's containerd shuts down cleanly and leaves
# its image store consistent on disk. Then make sure each store is in the
# named volume (`volumes:` in k3d-config.yaml) that outlives
# `k3d cluster delete`, so the next cluster starts with the images local.
#
# A cluster created from the current k3d-config.yaml already mounts those
# volumes, so stopping it is all this does. A cluster created before they
# existed keeps each store inside the node's anonymous /var/lib/rancher/k3s
# volume, which the delete takes with it, so copy the store across first: a
# one-time migration per cluster. The copy has to keep everything overlayfs
# snapshots are built from: whiteout device nodes, hardlinks, and
# trusted.overlay.* and security.capability xattrs. Without them images come
# back corrupted, with deleted files reappearing. The k3s image's cp is built
# without xattr support and busybox tar has none, so this installs GNU tar
# into alpine. Writing trusted.* xattrs needs CAP_SYS_ADMIN.
#
# Every failure returns nonzero, so with `set -e` teardown stops before
# `k3d cluster delete`. The cluster's own copy of its images is still there
# for a retry, or for --drop-images.
keep_node_images() {
    local node vol src
    # No mapfile and no expanding a possibly empty array: macOS ships bash
    # 3.2, which lacks the first and, under `set -u`, rejects the second.
    local -a mounts=() pending=()

    log "Stopping the cluster so each node's image store is consistent on disk..."
    if ! k3d cluster stop "${CLUSTER_NAME}" >/dev/null; then
        warn "Could not stop cluster '${CLUSTER_NAME}'."
        return 1
    fi

    while read -r node; do
        vol="$(node_image_volume "${node}")"
        if [[ "$(mounted_volume "${node}" "${CONTAINERD_DIR}")" == "${vol}" ]]; then
            ok "${node}: images already kept in volume ${vol}."
            continue
        fi
        src="$(mounted_volume "${node}" /var/lib/rancher/k3s)"
        if [[ -z "${src}" ]]; then
            warn "${node}: no /var/lib/rancher/k3s volume to copy its images from."
            return 1
        fi
        mounts+=(-v "${src}:/src/${vol}:ro" -v "${vol}:/dst/${vol}")
        pending+=("${vol}")
    done < <(docker ps -a --format '{{.Names}}' \
        | grep "^k3d-${CLUSTER_NAME}-\(server\|agent\)-" | sort)
    (( ${#pending[@]} == 0 )) && return 0

    log "Copying node image stores into named volumes (one-time migration)..."
    # Any earlier copy, partial or complete, is cleared before free space is
    # measured; otherwise a re-run after a failed copy counts that copy
    # against itself. All the volumes share the Docker VM's disk, so the
    # check compares the total size of the stores with free space on any one
    # of them.
    #
    # Each store is extracted into .migrating/ and moved to the volume's top
    # level only once tar has succeeded. An interrupted copy (Ctrl+C, a
    # crash) therefore leaves the volume looking empty to containerd, so a
    # cluster that mounts it just downloads again. A half-copied store whose
    # metadata lists layers that never arrived would break pod creation
    # instead.
    if ! docker run --rm --cap-add SYS_ADMIN "${mounts[@]}" alpine sh -euc '
        set -o pipefail
        need=0
        for v in "$@"; do
            find "/dst/$v" -mindepth 1 -delete
            need=$((need + $(du -sk "/src/$v/agent/containerd" | cut -f1)))
        done
        free=$(df -Pk "/dst/$1" | awk "NR == 2 {print \$4}")
        if [ "${need}" -ge "${free}" ]; then
            echo "    Copy needs $((need / 1048576)) GB free, $((free / 1048576)) GB available." >&2
            exit 1
        fi
        apk add --no-cache --quiet tar
        for v in "$@"; do
            stage="/dst/$v/.migrating"
            mkdir "${stage}"
            tar -C "/src/$v/agent/containerd" --xattrs --xattrs-include="*" \
                --numeric-owner -cf - . \
                | tar -C "${stage}" --xattrs --xattrs-include="*" \
                    --numeric-owner -xpf -
            find "${stage}" -mindepth 1 -maxdepth 1 -exec mv {} "/dst/$v/" \;
            chown "$(stat -c %u:%g "${stage}")" "/dst/$v"
            chmod "$(stat -c %a "${stage}")" "/dst/$v"
            rmdir "${stage}"
            echo "    $v: $(du -sh "/dst/$v" | cut -f1)"
        done
    ' sh "${pending[@]}"; then
        warn "Copying the image stores failed; removing the partial copies."
        docker volume rm "${pending[@]}" >/dev/null 2>&1 \
            || warn "Could not remove ${pending[*]}. Remove them before the next setup.sh."
        warn "The cluster is stopped, not deleted: re-run teardown, or pass"
        warn "--drop-images to delete it without its images."
        return 1
    fi
    ok "Node images copied into ${pending[*]}."
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

    log "  Destroying mitxonline stack..."
    destroy_stack "local-dev/apps/mitxonline/pulumi" "Dev" "mitxonline"

    # Destroy apps_infra before core (it depends on core stack)
    log "  Destroying apps_infra stack..."
    destroy_stack "local-dev/infra/apps_infra" "local-dev.apps-infra.Dev" "Apps infrastructure"

    # Destroy core stack (after apps_infra is gone)
    log "  Destroying core stack..."
    destroy_stack "local-dev/infra/core" "local-dev.core.Dev" "Core infrastructure"

    cd "${REPO_ROOT}"

    if ! $DROP_IMAGES; then
        keep_node_images
    fi

    # Now delete the cluster
    k3d cluster delete "${CLUSTER_NAME}"
    ok "Cluster deleted."
else
    warn "Cluster '${CLUSTER_NAME}' not found — nothing to delete."

    log "Checking for Pulumi state left behind by the missing cluster..."
    drop_orphan_stack "local-dev/apps/mitxonline/pulumi" "Dev" "mitxonline"
    drop_orphan_stack "local-dev/infra/apps_infra" "local-dev.apps-infra.Dev" "Apps infrastructure"
    drop_orphan_stack "local-dev/infra/core" "local-dev.core.Dev" "Core infrastructure"
    cd "${REPO_ROOT}"
fi

# start.sh's marker refers to the cluster we just deleted.
rm -f "${REPO_ROOT}/local-dev/infra/.pulumi/cluster-id"

if $DROP_IMAGES; then
    log "Removing kept node images (--drop-images)..."
    kept_image_volumes | xargs -r docker volume rm >/dev/null
    ok "Node image volumes removed."
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
if ! $DROP_IMAGES && [[ -n "$(kept_image_volumes)" ]]; then
    echo "Node images kept for the next setup.sh in: $(kept_image_volumes | tr '\n' ' ')"
    echo "(teardown.sh --drop-images removes them.)"
fi
echo "Teardown complete. Run ./local-dev/scripts/setup.sh to start fresh."
