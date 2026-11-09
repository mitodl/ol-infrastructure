#!/bin/bash -x
# Publish a prerelease of the generated client under the npm `next` dist-tag.
#
# Version: <build date CalVer>-dev.<unix time>.g<source sha7>. It is never
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
# GNU date (node:*-slim is Debian). %-m and %-d drop the zero padding, as the
# stable CalVer does.
version="$(date -u +%Y.%-m.%-d)-dev.$(date -u +%s).g${SOURCE_SHA:0:7}"
yarn version "$version"
yarn build
echo "Publishing prerelease $version"
yarn npm publish --access public --tag next
