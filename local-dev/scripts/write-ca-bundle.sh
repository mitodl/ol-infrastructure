#!/usr/bin/env bash
# write-ca-bundle.sh — Write the host trust bundle the apps-infra stack runs with.
#
# The SCIM remote-provider resource is the one resource in that stack that
# reaches Keycloak from Python (requests) rather than through pulumi_keycloak,
# so it has to trust the mkcert root that signs sso.ol.<domain>. The Tiltfile
# points REQUESTS_CA_BUNDLE at the file this writes.
#
# certifi's public roots are concatenated with the mkcert root because
# REQUESTS_CA_BUNDLE REPLACES the trust store rather than adding to it: a bare
# rootCA.pem would break every public-CA call in the same program.
#
# Run as a prelude to `pulumi up`, not from setup.sh, because `pulumi up
# --refresh` calls the dynamic provider's read() before it runs the program, and
# that call needs the bundle already in place. Writing it per build also keeps
# its public roots matching the venv's certifi, which a bundle generated once
# does not.
#
# Usage:
#   ./local-dev/scripts/write-ca-bundle.sh
#
# Prints the path it wrote, so a caller can use it directly:
#   REQUESTS_CA_BUNDLE="$(./local-dev/scripts/write-ca-bundle.sh)"

set -euo pipefail

REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
VENV_PYTHON="${REPO_ROOT}/.venv/bin/python"

if [[ ! -x "${VENV_PYTHON}" ]]; then
    echo "No venv at ${VENV_PYTHON}. Run 'uv sync'." >&2
    exit 1
fi

# Delegated to modules/tls.py so the host bundle and the in-cluster one
# (local-dev-ca-bundle, apisix-trusted-ca) stay one definition: certifi is
# resolved from this venv, which is the one the Pulumi programs run under.
PYTHONPATH="${REPO_ROOT}/local-dev/infra" "${VENV_PYTHON}" -c '
import sys
from pathlib import Path

from modules.tls import write_host_ca_bundle

print(write_host_ca_bundle(Path(sys.argv[1])))
' "${REPO_ROOT}/local-dev/certs/rootCA.pem"
