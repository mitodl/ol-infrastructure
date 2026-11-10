#!/bin/bash -x
# Publish a prerelease of the generated client under the npm `next` dist-tag.
#
# Version: <build date CalVer>.0-dev.<unix time>.g<source sha7>, e.g.
# 2026.1110.0-dev.1762790000.gabc1234: a prerelease of the day's first stable
# client, so it sorts above every earlier day's stable one. It is never
# committed; the stable channel (api-clients-publish-node.sh) owns VERSION and
# `latest`. Unique per build, so a re-run publishes a new version instead of
# hitting npm's 403 on an existing one. The `g` keeps an all-digit short sha
# from being a numeric semver identifier, which may not have a leading zero.
set -euo pipefail

export YARN_NPM_PUBLISH_REGISTRY=https://registry.npmjs.org/
set +x
export YARN_NPM_AUTH_TOKEN="${NPM_TOKEN}"
set -x
corepack enable
COREPACK_ENABLE_DOWNLOAD_PROMPT=0 yarn install --immutable
# GNU date (node:*-slim is Debian). %-m%d is the stable CalVer's MM0D (bumpver
# YYYY.MM0D.INC0): month unpadded, day zero-padded, so the minor never has a
# leading zero.
version="$(date -u +%Y.%-m%d).0-dev.$(date -u +%s).g${SOURCE_SHA:0:7}"
yarn version "$version"
yarn build
echo "Publishing prerelease $version"
yarn npm publish --access public --tag next
