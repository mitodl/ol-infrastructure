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
# --init: without it prove runs as PID 1, nothing reaps the exited nginx, and
# Test::Nginx (which checks liveness with `kill 0`) takes the zombie for a
# running server -- every test block then sat out a 3s shutdown timeout and
# ~3s of kill retries, which was ~220s of a ~225s run.
exec docker run --rm --init --user root "${image}" "$@"
