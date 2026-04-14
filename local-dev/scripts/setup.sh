#!/usr/bin/env bash
# setup.sh — One-time bootstrap for the MIT Learn local development environment.
#
# What this script does:
#   1. Validates prerequisites (Docker, kubectl, k3d, tilt, helm, mkcert, pulumi)
#   2. Creates the k3d cluster (mit-learn-dev) with local image registry
#   3. Generates mkcert TLS certificates for all local .dev hostnames
#   4. Adds /etc/hosts entries for all local hostnames
#
# What this script does NOT do:
#   - Install in-cluster resources (Pulumi owns all of those; run `tilt up`)
#
# Usage:
#   ./local-dev/scripts/setup.sh [--skip-hosts] [--skip-certs] [--reinstall-tools]

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
K3D_CONFIG="${REPO_ROOT}/local-dev/cluster/k3d-config.yaml"
CLUSTER_NAME="mit-learn-dev"

# ---------------------------------------------------------------------------
# All local hostnames — must be listed explicitly; /etc/hosts has no wildcards.
# Pattern: production domain with .edu → .dev
# ---------------------------------------------------------------------------
HOSTS=(
    # mit-learn backend (Django/granian)
    "api.learn.mit.dev"
    # mit-learn frontend (Next.js)
    "learn.mit.dev"
    # learn-ai
    "ai.learn.mit.dev"
    # mitxonline
    "mitxonline.mit.dev"
    # odl-video-service
    "video.odl.mit.dev"
    # Keycloak SSO
    "sso.ol.mit.dev"
)

# mkcert wildcard SANs — one wildcard per subdomain level needed.
MKCERT_DOMAINS=(
    "*.mit.dev"           # learn.mit.dev, mitxonline.mit.dev
    "*.learn.mit.dev"     # api.learn.mit.dev, ai.learn.mit.dev
    "*.mitxonline.mit.dev"
    "*.ol.mit.dev"        # sso.ol.mit.dev
    "*.odl.mit.dev"       # video.odl.mit.dev
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

# Merge kubeconfig
k3d kubeconfig merge "${CLUSTER_NAME}" --kubeconfig-merge-default &>/dev/null
kubectl config use-context "k3d-${CLUSTER_NAME}" &>/dev/null
ok "kubectl context set to k3d-${CLUSTER_NAME}"

# ---------------------------------------------------------------------------
# 3. TLS certificates via mkcert
# ---------------------------------------------------------------------------
if $SKIP_CERTS; then
    warn "Skipping certificate generation (--skip-certs)."
else
    log "Generating mkcert TLS certificates..."

    mkdir -p "${CERT_DIR}"

    # Trust the mkcert CA system-wide (requires sudo on Linux for /usr/local/share)
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
    cp "$(mkcert -CAROOT)/rootCA-key.pem" "${CERT_DIR}/rootCA-key.pem"
    ok "mkcert CA cert copied to ${CERT_DIR}/rootCA.pem"
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

    HOSTS_BLOCK_START="# BEGIN mit-learn-dev local-dev"
    HOSTS_BLOCK_END="# END mit-learn-dev local-dev"

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
fi

# ---------------------------------------------------------------------------
# 5. Pulumi stack prerequisites
# ---------------------------------------------------------------------------
log "Checking Pulumi local-dev/infra stack..."

INFRA_DIR="${REPO_ROOT}/local-dev/infra"
if [[ -d "${INFRA_DIR}" ]]; then
    (
        cd "${INFRA_DIR}"
        # Use local filesystem state (no AWS credentials needed)
        if ! pulumi stack ls --json 2>/dev/null | python3 -c "import json,sys; stacks=[s['name'] for s in json.load(sys.stdin)]; sys.exit(0 if 'local-dev.infra.Dev' in stacks else 1)" 2>/dev/null; then
            pulumi stack init local-dev.infra.Dev --secrets-provider=passphrase 2>/dev/null || true
        fi
    )
    ok "Pulumi infra stack ready."
fi

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------
cat <<EOF

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  MIT Learn local dev setup complete!

  Next steps:
    1. Copy and edit your personal config:
         cp tilt_config.json.example tilt_config.json
       (Add openai_api_key if you want AI/embedding features)

    2. Start the full stack:
         tilt up

    3. Tilt UI: http://localhost:10350

  Local hostnames (all resolve to 127.0.0.1):
$(for h in "${HOSTS[@]}"; do echo "    https://${h}"; done)

  To tear down:
    ./local-dev/scripts/teardown.sh
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
EOF
