#!/usr/bin/env bash
# kc-theme-image.sh — run local Keycloak with an unreleased ol-keycloakify theme
#
# Builds the theme from an ol-keycloakify checkout into a Keycloak image
# (local-dev/keycloak/Dockerfile), pushes it to the k3d registry, and points the
# core stack at it through `keycloak_image` in tilt_config.json. A running
# `tilt up` picks the change up on its own: local-infra-core re-applies and the
# operator rolls keycloak-0, which takes a minute or two and logs every local
# session out.
#
# Usage:
#   ./local-dev/scripts/kc-theme-image.sh [CHECKOUT]   # default: ../ol-keycloakify
#   ./local-dev/scripts/kc-theme-image.sh --reset      # back to the published image
#
# The default checkout is the ol-keycloakify sibling of this repo, the same
# workspace layout the root Tiltfile assumes (MITOL_WORKSPACE_ROOT overrides the
# workspace directory there and here). The image tag is the checkout's short
# HEAD plus a hash of its uncommitted changes and of the Dockerfile, so each
# distinct input gets its own tag and the operator rolls once per change.
#
# Environment:
#   DOCKER                  docker CLI to invoke (default: docker)
#   LOCAL_DEV_REGISTRY_PUSH registry as the host reaches it (default: localhost:5001)
#   LOCAL_DEV_REGISTRY_PULL registry as the cluster nodes reach it
#                           (default: k3d-registry.localhost:5000)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
DOCKERFILE="${REPO_ROOT}/local-dev/keycloak/Dockerfile"
TILT_CONFIG="${REPO_ROOT}/tilt_config.json"
CORE_STACK="${REPO_ROOT}/local-dev/infra/core/__main__.py"

DOCKER="${DOCKER:-docker}"
# The daemon treats `localhost` as insecure automatically but insists on TLS for
# `k3d-registry.localhost`, so pushes go to the former; in-cluster pulls use the
# k3s registry mirror name, which is also how Tilt-built images are referenced.
PUSH_REGISTRY="${LOCAL_DEV_REGISTRY_PUSH:-localhost:5001}"
PULL_REGISTRY="${LOCAL_DEV_REGISTRY_PULL:-k3d-registry.localhost:5000}"
REPO="keycloak"

die() { echo "error: $*" >&2; exit 1; }

usage() { sed -n '2,/^$/p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }

write_keycloak_image() {
  local value="$1" tmp
  tmp="$(mktemp)"
  if [[ -f "${TILT_CONFIG}" ]]; then
    jq --arg v "${value}" '.keycloak_image = $v' "${TILT_CONFIG}" > "${tmp}"
  else
    jq -n --arg v "${value}" '{keycloak_image: $v}' > "${tmp}"
  fi
  # cat, not mv: keep the file's mode and identity (mktemp creates 0600).
  cat "${tmp}" > "${TILT_CONFIG}" && rm -f "${tmp}"
}

for tool in "${DOCKER}" jq curl git shasum; do
  command -v "${tool}" >/dev/null || die "${tool} is required"
done

case "${1:-}" in
  -h|--help) usage; exit 0 ;;
  --reset)
    write_keycloak_image ""
    echo "keycloak_image cleared in ${TILT_CONFIG}. If Tilt is running, local-infra-core re-applies and keycloak-0 rolls back to the published image (a minute or two)."
    exit 0
    ;;
  -*) die "unknown option: $1 (see --help)" ;;
esac

WORKSPACE_ROOT="${MITOL_WORKSPACE_ROOT:-${REPO_ROOT}/..}"
CHECKOUT="${1:-${WORKSPACE_ROOT}/ol-keycloakify}"
[[ -f "${CHECKOUT}/package.json" && -f "${CHECKOUT}/vite.config.ts" ]] \
  || die "${CHECKOUT} does not look like an ol-keycloakify checkout (pass the path as the first argument)"
CHECKOUT="$(cd "${CHECKOUT}" && pwd)"

curl -sf "http://${PUSH_REGISTRY}/v2/" >/dev/null \
  || die "no registry at ${PUSH_REGISTRY}; is the k3d cluster running? (./local-dev/scripts/start.sh)"

# The Dockerfile's base must be the image the core stack runs by default, so
# read the digest from the one place it is pinned rather than duplicating it.
BASE_DIGEST="$(grep -o 'sha256:[0-9a-f]\{64\}' "${CORE_STACK}" | head -1)"
[[ -n "${BASE_DIGEST}" ]] || die "no mitodl/keycloak digest found in ${CORE_STACK}"

# Tag: short HEAD plus a hash of the tracked modifications, untracked files
# (gitignored ones excluded), and the Dockerfile, so edits made without
# committing still get a fresh tag and a fresh rollout.
head_sha="$(git -C "${CHECKOUT}" rev-parse --short HEAD)"
input_hash="$(
  {
    git -C "${CHECKOUT}" diff HEAD --
    git -C "${CHECKOUT}" ls-files --others --exclude-standard -z \
      | (cd "${CHECKOUT}" && xargs -0 cat 2>/dev/null)
    cat "${DOCKERFILE}"
    echo "${BASE_DIGEST}"
  } | shasum -a 256 | cut -c1-8
)"
TAG="${head_sha}-${input_hash}"
PUSH_REF="${PUSH_REGISTRY}/${REPO}:${TAG}"
PULL_REF="${PULL_REGISTRY}/${REPO}:${TAG}"

echo "Building ${PUSH_REF} from ${CHECKOUT} ..."
# The base image is linux/amd64 only. --provenance=false --sbom=false keeps the
# push a single image manifest: BuildKit's default attestation turns it into an
# OCI index, and containerd on the arm64 k3d nodes then looks for an arm64
# entry, finds none, and fails the pull with "no match for platform in
# manifest". A plain amd64 manifest is pulled as-is, like the published base.
"${DOCKER}" build \
  --platform linux/amd64 \
  --provenance=false --sbom=false \
  --build-arg "BASE_IMAGE=mitodl/keycloak@${BASE_DIGEST}" \
  -f "${DOCKERFILE}" \
  -t "${PUSH_REF}" \
  "${CHECKOUT}"
"${DOCKER}" push "${PUSH_REF}"

media="$(curl -sf "http://${PUSH_REGISTRY}/v2/${REPO}/manifests/${TAG}" \
  -H 'Accept: application/vnd.oci.image.manifest.v1+json' \
  -H 'Accept: application/vnd.docker.distribution.manifest.v2+json' \
  -H 'Accept: application/vnd.oci.image.index.v1+json' \
  -H 'Accept: application/vnd.docker.distribution.manifest.list.v2+json' \
  | jq -r '.mediaType // empty')"
case "${media}" in
  *index*|*manifest.list*)
    die "${PUSH_REF} was pushed as a multi-manifest index (${media}); the cluster nodes cannot pull it. Is a buildx driver overriding --provenance/--sbom?" ;;
esac

write_keycloak_image "${PULL_REF}"
cat <<EOF
keycloak_image = ${PULL_REF} written to ${TILT_CONFIG}

If Tilt is running it re-applies local-infra-core now and the operator rolls
keycloak-0 (a minute or two). Every local session is logged out by the restart.
Follow along with:
  kubectl -n local-infra rollout status statefulset/keycloak
Return to the published image with:
  ${BASH_SOURCE[0]} --reset
EOF
