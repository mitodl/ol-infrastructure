#!/bin/bash -x
# publish the npm package to npmjs.org

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
