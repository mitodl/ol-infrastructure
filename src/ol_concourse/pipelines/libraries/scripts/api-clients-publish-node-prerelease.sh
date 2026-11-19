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
#
# Published only when the built client differs from both the current `next`
# and `latest` packages, so a spec edit that leaves the generated client as it
# was publishes nothing. When it matches `latest` but not `next` (main reverted
# an API change that `next` still has), `next` is moved to the `latest`
# version, so `@next` never serves an API main no longer has.
#
# A build whose specs are no longer main's (a re-run of an older build) touches
# neither tag: api-clients-check-source-current.sh leaves .git/source-stale in
# the client repo, three levels up from this package directory.
set -euo pipefail

if [ -e ../../../.git/source-stale ]; then
	echo "This build is older than the specs on main: publishing nothing and leaving next as it is."
	exit 0
fi

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
# npm, not yarn's version command: yarn spawns git whenever the checkout has
# a .git, and node:*-slim ships without git ("Process git failed to spawn").
npm pkg set "version=$version"
yarn build
# dist/ is everything the client is: the package ships dist/ and LICENSE only,
# and nothing in dist/ carries the package version.
name="$(node -p "require('./package.json').name")"
for channel in next latest; do
	published="$(mktemp -d)"
	if npm pack "$name@$channel" --pack-destination "$published" >/dev/null 2>&1 &&
		tar -xzf "$published"/*.tgz -C "$published" &&
		diff -r -q dist "$published/package/dist" >/dev/null; then
		echo "This client is the one already published as $name@$channel: publishing nothing."
		if [ "$channel" = latest ]; then
			# The version of the tarball just compared, not a fresh `npm view`,
			# so the tag lands on the contents that matched.
			latest_version="$(node -p "require('$published/package/package.json').version")"
			echo "Moving $name@next to $latest_version."
			yarn npm tag add "$name@$latest_version" next
		fi
		exit 0
	fi
done
echo "Publishing prerelease $version"
yarn npm publish --access public --tag next
