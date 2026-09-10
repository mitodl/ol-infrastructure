#!/usr/bin/env bash
# Build and run the test-nginx suite for this repo's APISIX Lua.
#
#   tests/apisix_testnginx/run.sh
#
# Build context is the repository root so the Dockerfile can copy the Lua from
# src/ rather than from a second copy kept in this directory.
set -euo pipefail

repo_root=$(git rev-parse --show-toplevel)
image=${APISIX_TESTNGINX_IMAGE:-ol-apisix-testnginx}

cd "${repo_root}"
docker build -t "${image}" -f tests/apisix_testnginx/Dockerfile .
# --user root: the harness writes t/servroot and needs to bind a listener.
exec docker run --rm --user root "${image}" "$@"
