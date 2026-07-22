#!/usr/bin/env bash
# setup.sh — One-time bootstrap for the MIT Learn local development environment.
#
# What this script does:
#   1. Validates prerequisites (Docker, kubectl, k3d, tilt, helm, mkcert, pulumi)
#   2. Creates the k3d cluster (local-dev) with local image registry
#   3. Generates mkcert TLS certificates for all local .dev hostnames
#   4. Adds /etc/hosts entries for all local hostnames
#
# What this script does NOT do:
#   - Install in-cluster resources (Pulumi owns all of those; run `tilt up`)
#
# Usage:
#   ./local-dev/scripts/setup.sh [--skip-hosts] [--skip-certs]

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
K3D_CONFIG="${REPO_ROOT}/local-dev/cluster/k3d-config.yaml"
CLUSTER_NAME="local-dev"

# ---------------------------------------------------------------------------
# Root domain configuration
# ---------------------------------------------------------------------------
# Override by setting LOCAL_DEV_ROOT_DOMAIN before running this script:
#   LOCAL_DEV_ROOT_DOMAIN=mycompany.dev ./local-dev/scripts/setup.sh
ROOT_DOMAIN="${LOCAL_DEV_ROOT_DOMAIN:-mit.dev}"

# ---------------------------------------------------------------------------
# All local hostnames — must be listed explicitly; /etc/hosts has no wildcards.
# ---------------------------------------------------------------------------
HOSTS=(
    # mit-learn backend (Django/granian)
    "api.learn.${ROOT_DOMAIN}"
    # mit-learn frontend (Next.js)
    "learn.${ROOT_DOMAIN}"
    # learn-ai
    "ai.learn.${ROOT_DOMAIN}"
    # mitxonline
    "mitxonline.${ROOT_DOMAIN}"
    # odl-video-service
    "video.odl.${ROOT_DOMAIN}"
    # Keycloak SSO
    "sso.ol.${ROOT_DOMAIN}"
    # Mailpit (captured outbound email)
    "mail.${ROOT_DOMAIN}"
)

# mkcert wildcard SANs — one wildcard per subdomain level needed.
MKCERT_DOMAINS=(
    "*.${ROOT_DOMAIN}"                # learn.*, mitxonline.*
    "*.learn.${ROOT_DOMAIN}"          # api.learn.*, ai.learn.*
    "*.mitxonline.${ROOT_DOMAIN}"
    "*.ol.${ROOT_DOMAIN}"             # sso.ol.*
    "*.odl.${ROOT_DOMAIN}"            # video.odl.*
)

# Output cert files (mkcert names them from the first domain, replacing * with _)
CERT_DIR="${REPO_ROOT}/local-dev/certs"
CERT_FILE="${CERT_DIR}/local-dev.pem"
KEY_FILE="${CERT_DIR}/local-dev-key.pem"

# ---------------------------------------------------------------------------
# Flags
# ---------------------------------------------------------------------------
SKIP_HOSTS=false
SKIP_CERTS=false

while [[ $# -gt 0 ]]; do
    case "$1" in
        --skip-hosts)   SKIP_HOSTS=true;  shift ;;
        --skip-certs)   SKIP_CERTS=true;  shift ;;
        -h|--help)
            echo "Usage: setup.sh [--skip-hosts] [--skip-certs]"
            exit 0
            ;;
        *) echo "Unknown argument: $1" >&2; exit 1 ;;
    esac
done

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
log()  { echo "▶ $*"; }
ok()   { echo "  ✓ $*"; }
warn() { echo "  ⚠ $*"; }
err()  { echo "  ✗ $*" >&2; exit 1; }

need_cmd() {
    command -v "$1" &>/dev/null || err "'$1' not found. Install it and re-run setup.sh."
}

version_ge() {
    # Returns 0 if version $1 >= $2 (both as dot-separated integers)
    python3 -c "
import sys
a = tuple(int(x) for x in '${1}'.split('.'))
b = tuple(int(x) for x in '${2}'.split('.'))
sys.exit(0 if a >= b else 1)
"
}

install_tool_hint() {
    local tool="$1"
    case "$(uname)" in
        Darwin)  echo "  → brew install ${tool}" ;;
        Linux)   echo "  → See https://github.com/${tool}/${tool}/releases or package manager" ;;
    esac
}

# Returns 0 if running inside WSL2.
is_wsl() {
    [[ -n "${WSL_DISTRO_NAME:-}" ]] || grep -qi microsoft /proc/version 2>/dev/null
}

# Ensures /etc/wsl.conf has [network] generateHosts = false so that WSL does
# not overwrite /etc/hosts on every restart.  Prints 'ok' (already set) or
# 'changed' (file was updated) and exits with 0 in both cases.
_wsl_conf_disable_generate_hosts() {
    local tmppy
    tmppy=$(mktemp /tmp/wsl_conf_update.XXXXXX.py)
    # Single-quoted heredoc prevents bash from expanding anything inside.
    cat > "$tmppy" << 'PYEOF'
import re, sys

path = sys.argv[1]
try:
    with open(path) as f:
        content = f.read()
except FileNotFoundError:
    content = ""

if re.search(r"generateHosts\s*=\s*false", content, re.IGNORECASE):
    print("ok")
    sys.exit(0)

if re.search(r"generateHosts\s*=\s*\w+", content, re.IGNORECASE):
    content = re.sub(
        r"(generateHosts\s*=\s*)\w+", r"\1false", content, flags=re.IGNORECASE
    )
elif re.search(r"^\[network\]", content, re.MULTILINE | re.IGNORECASE):
    content = re.sub(
        r"(\[network\][^\n]*\n)",
        lambda m: m.group(0) + "generateHosts = false\n",
        content,
        flags=re.IGNORECASE,
    )
else:
    if content and not content.endswith("\n"):
        content += "\n"
    content += "\n[network]\ngenerateHosts = false\n"

with open(path, "w") as f:
    f.write(content)
print("changed")
PYEOF
    local result
    result=$(sudo python3 "$tmppy" /etc/wsl.conf)
    rm -f "$tmppy"
    echo "$result"
}

# Attempts to write $HOSTS_BLOCK to the Windows hosts file (readable from WSL
# at the standard mount path).  Falls back to printed instructions when the
# file is not writable (requires Windows admin elevation).
_update_windows_hosts() {
    local block="$1"
    local win_hosts
    win_hosts=$(wslpath -u 'C:\Windows\System32\drivers\etc\hosts' 2>/dev/null) \
        || win_hosts="/mnt/c/Windows/System32/drivers/etc/hosts"

    if [[ ! -f "$win_hosts" ]]; then
        warn "Windows hosts file not found at '${win_hosts}'. Add entries manually."
        return
    fi

    # Idempotent: remove any existing block first.
    if grep -q "# BEGIN local-dev local-dev" "$win_hosts" 2>/dev/null; then
        python3 -c "
import re
with open('${win_hosts}', 'r') as f:
    content = f.read()
content = re.sub(
    r'# BEGIN local-dev local-dev.*?# END local-dev local-dev\n?',
    '',
    content,
    flags=re.DOTALL,
)
content = content.rstrip('\n') + '\n'
with open('${win_hosts}', 'w') as f:
    f.write(content)
" 2>/dev/null || true
    fi

    if echo "$block" >> "$win_hosts" 2>/dev/null; then
        ok "Windows hosts file updated (${win_hosts})."
    else
        warn "Windows hosts file is not writable (requires Windows admin elevation)."
        warn "Add the block below to C:\\Windows\\System32\\drivers\\etc\\hosts manually,"
        warn "or paste into an elevated Windows PowerShell:"
        echo ""
        printf "    Add-Content \$env:windir\\\\System32\\\\drivers\\\\etc\\\\hosts -Value @\"\n"
        echo "$block"
        printf '"@\n'
        echo ""
    fi
}

# ---------------------------------------------------------------------------
# 1. Validate prerequisites
# ---------------------------------------------------------------------------
log "Checking prerequisites..."

need_cmd docker
need_cmd kubectl
need_cmd k3d
need_cmd tilt
need_cmd helm
need_cmd mkcert
need_cmd pulumi
need_cmd python3
need_cmd uv

# Warn if Python 3.14+ is the active interpreter; Pulumi's asyncio runtime
# has known breakages on 3.14 due to event-loop API changes.
PYTHON_MINOR=$(python3 -c "import sys; print(sys.version_info.minor)")
PYTHON_MAJOR=$(python3 -c "import sys; print(sys.version_info.major)")
if [[ "${PYTHON_MAJOR}" -eq 3 && "${PYTHON_MINOR}" -ge 14 ]]; then
    warn "Python 3.${PYTHON_MINOR} detected. Pulumi requires Python <=3.13."
    warn "Install Python 3.12 and set it as the default, or use 'uv python pin 3.12'."
fi

# Docker must be running
docker info &>/dev/null || err "Docker is not running. Start Docker Desktop or the Docker daemon."

# Docker memory check (warn if < 8 GB)
DOCKER_MEM_BYTES=$(docker info --format '{{.MemTotal}}' 2>/dev/null || echo 0)
DOCKER_MEM_GB=$(python3 -c "print(${DOCKER_MEM_BYTES} // (1024**3))")
if [[ "$DOCKER_MEM_GB" -lt 8 ]]; then
    warn "Docker is configured with ${DOCKER_MEM_GB} GB RAM. 8 GB recommended for full stack."
fi

ok "Prerequisites satisfied (Docker ${DOCKER_MEM_GB} GB RAM)"

# ---------------------------------------------------------------------------
# 2. Create k3d cluster
# ---------------------------------------------------------------------------
log "Setting up k3d cluster '${CLUSTER_NAME}'..."

if k3d cluster list 2>/dev/null | grep -q "^${CLUSTER_NAME}"; then
    ok "Cluster '${CLUSTER_NAME}' already exists — skipping creation."
else
    k3d cluster create --config "${K3D_CONFIG}"
    ok "Cluster '${CLUSTER_NAME}' created."
fi

# Merge into default kubeconfig and rename the context to 'local-dev' for clarity.
k3d kubeconfig merge "${CLUSTER_NAME}" --kubeconfig-merge-default &>/dev/null
# k3d names the context k3d-<cluster>; rename to the canonical 'local-dev'.
if kubectl config get-contexts "local-dev" &>/dev/null; then
    # Context already exists with the right name — ensure it points at this cluster.
    kubectl config delete-context "local-dev" &>/dev/null || true
fi
kubectl config rename-context "k3d-${CLUSTER_NAME}" "local-dev" &>/dev/null
kubectl config use-context "local-dev"
ok "kubectl context 'local-dev' registered and active."

# ---------------------------------------------------------------------------
# 3. TLS certificates via mkcert
# ---------------------------------------------------------------------------
if $SKIP_CERTS; then
    warn "Skipping certificate generation (--skip-certs)."
else
    log "Generating mkcert TLS certificates..."

    mkdir -p "${CERT_DIR}"

    # mkcert calls keytool on every invocation to check/update the Java trust store.
    # On sdkman-managed JDKs the keytool binary can lack execute permission, aborting
    # the script. Inject a no-op keytool shim at the front of PATH for all mkcert
    # calls in this block, and clear JAVA_HOME so the resolved path isn't used either.
    _KEYTOOL_SHIM=$(mktemp -d)
    printf '#!/usr/bin/env bash\nexit 0\n' > "${_KEYTOOL_SHIM}/keytool"
    chmod +x "${_KEYTOOL_SHIM}/keytool"
    PATH="${_KEYTOOL_SHIM}:${PATH}"
    export JAVA_HOME=""

    mkcert -install
    ok "mkcert CA trusted."

    # Generate wildcard certs covering all local hostnames.
    # Use explicit output file names so Pulumi can reliably reference them.
    mkcert \
        -cert-file "${CERT_FILE}" \
        -key-file "${KEY_FILE}" \
        "${MKCERT_DOMAINS[@]}"
    ok "Certificates written to ${CERT_DIR}/"

    # Also copy the mkcert root CA to certs/ so Pulumi can reference it.
    cp "$(mkcert -CAROOT)/rootCA.pem" "${CERT_DIR}/rootCA.pem"
    ok "mkcert CA cert copied to ${CERT_DIR}/rootCA.pem"

    # WSL: the mkcert CA installed above only covers the Linux trust store.
    # Windows browsers need the same root CA imported into Windows.
    if is_wsl; then
        win_ca_path=$(wslpath -w "${CERT_DIR}/rootCA.pem" 2>/dev/null) \
            || win_ca_path="${CERT_DIR}/rootCA.pem"
        warn "WSL detected: Windows browsers need the mkcert root CA trusted on Windows."
        warn "Run in an elevated Windows PowerShell:"
        echo ""
        echo "    certutil -addstore Root '${win_ca_path}'"
        echo ""
    fi

    rm -rf "${_KEYTOOL_SHIM}"
fi

# ---------------------------------------------------------------------------
# 4. /etc/hosts entries
# ---------------------------------------------------------------------------
if $SKIP_HOSTS; then
    warn "Skipping /etc/hosts update (--skip-hosts)."
else
    log "Updating /etc/hosts..."

    # k3d load balancer always listens on 127.0.0.1 for the exposed ports
    INGRESS_IP="127.0.0.1"

    HOSTS_BLOCK_START="# BEGIN local-dev local-dev"
    HOSTS_BLOCK_END="# END local-dev local-dev"

    # Build the new hosts block
    HOSTS_BLOCK="${HOSTS_BLOCK_START}"$'\n'
    for host in "${HOSTS[@]}"; do
        HOSTS_BLOCK+="${INGRESS_IP}  ${host}"$'\n'
    done
    HOSTS_BLOCK+="${HOSTS_BLOCK_END}"

    if grep -q "${HOSTS_BLOCK_START}" /etc/hosts; then
        # Remove existing block and replace
        sudo python3 -c "
import re, sys
with open('/etc/hosts', 'r') as f:
    content = f.read()
content = re.sub(
    r'${HOSTS_BLOCK_START}.*?${HOSTS_BLOCK_END}\n?',
    '',
    content,
    flags=re.DOTALL,
)
content = content.rstrip('\n') + '\n'
with open('/etc/hosts', 'w') as f:
    f.write(content)
"
    fi

    echo "${HOSTS_BLOCK}" | sudo tee -a /etc/hosts > /dev/null
    ok "/etc/hosts updated with ${#HOSTS[@]} entries."

    # WSL: prevent /etc/hosts from being regenerated on WSL restart, and mirror
    # the same entries into the Windows hosts file so that Windows browsers can
    # resolve the .dev hostnames (Docker Desktop forwards 127.0.0.1 from WSL).
    if is_wsl; then
        log "WSL detected: updating wsl.conf and Windows hosts file..."
        wsl_result=$(_wsl_conf_disable_generate_hosts)
        if [[ "$wsl_result" == "ok" ]]; then
            ok "wsl.conf already has generateHosts = false."
        else
            warn "Updated /etc/wsl.conf: [network] generateHosts = false"
            warn "Run 'wsl --shutdown' in Windows PowerShell, then reopen this terminal."
        fi
        _update_windows_hosts "${HOSTS_BLOCK}"
    fi
fi

# ---------------------------------------------------------------------------
# 5. Pulumi stack prerequisites
# ---------------------------------------------------------------------------
log "Checking Pulumi stack prerequisites..."

# Shared state backend lives at local-dev/infra/.pulumi (referenced as
# file://../.pulumi from each sub-project Pulumi.yaml).
mkdir -p "${REPO_ROOT}/local-dev/infra/.pulumi"

# PULUMI_CONFIG_PASSPHRASE="" uses an empty passphrase for the local secrets
# provider — avoids interactive prompts in local dev.
export PULUMI_CONFIG_PASSPHRASE=""

# Init the core stack if it doesn't exist yet.
(
    cd "${REPO_ROOT}/local-dev/infra/core"
    if ! pulumi stack ls --json 2>/dev/null \
        | python3 -c "import json,sys; stacks=[s['name'] for s in json.load(sys.stdin)]; sys.exit(0 if 'local-dev.core.Dev' in stacks else 1)" 2>/dev/null; then
        log "  Initialising core stack..."
        pulumi stack init local-dev.core.Dev --secrets-provider=passphrase
    fi
)

# Init the apps_infra stack if it doesn't exist yet.
(
    cd "${REPO_ROOT}/local-dev/infra/apps_infra"
    if ! pulumi stack ls --json 2>/dev/null \
        | python3 -c "import json,sys; stacks=[s['name'] for s in json.load(sys.stdin)]; sys.exit(0 if 'local-dev.apps-infra.Dev' in stacks else 1)" 2>/dev/null; then
        log "  Initialising apps_infra stack..."
        pulumi stack init local-dev.apps-infra.Dev --secrets-provider=passphrase
    fi
)

ok "Pulumi stacks ready."

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------
cat <<EOF

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  MIT Learn local dev setup complete!

  Next steps:
    1. Copy and edit your personal config:
         cp tilt_config.json.example tilt_config.json
       (App env vars / API keys go in gitignored app-env.local.yaml files instead —
        see "Local Configuration Overrides" in local-dev/README.md)

    2. Start the full stack:
         tilt up

    3. Tilt UI: http://localhost:10350

  Local hostnames (all resolve to 127.0.0.1):
$(for h in "${HOSTS[@]}"; do echo "    https://${h}"; done)

  To tear down:
    ./local-dev/scripts/teardown.sh
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
EOF
