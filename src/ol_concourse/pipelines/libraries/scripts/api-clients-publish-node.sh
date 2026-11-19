#!/bin/bash -x
# publish the npm package to npmjs.org

# generate-clients pushes the client repo's HEAD even when the client did not
# change, and then this job sees a commit it has not built if that HEAD is not
# a generated one (a merged clients-repo PR). Its version is already on npm,
# and npm refuses to publish a version twice (403), so stop here instead.
name="$(node -p "require('./package.json').name")"
version="$(node -p "require('./package.json').version")"
if [ -n "$(npm view "$name@$version" version 2>/dev/null)" ]; then
	echo "$name@$version is already on npm: nothing to publish."
	exit 0
fi

export YARN_NPM_PUBLISH_REGISTRY=https://registry.npmjs.org/
set +x
export YARN_NPM_AUTH_TOKEN="${NPM_TOKEN}"
set -x
corepack enable
COREPACK_ENABLE_DOWNLOAD_PROMPT=0 yarn install --immutable
yarn build
# OK so this is vaguely gross but it will do :)
new_version=$(cat ../../../VERSION)
echo "Publishing version $new_version"
yarn npm publish --access public
