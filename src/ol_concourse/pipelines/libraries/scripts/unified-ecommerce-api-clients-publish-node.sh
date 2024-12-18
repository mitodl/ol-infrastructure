#!/bin/bash -x
# publish the npm package to npmjs.org

echo "//registry.npmjs.org/:_authToken=((npm_publish.npmjs_token))" > .npmrc
COREPACK_ENABLE_DOWNLOAD_PROMPT=0
export COREPACK_ENABLE_DOWNLOAD_PROMPT
corepack enable
yarn install --immutable
yarn build
# OK so this is vaguely gross but it will do :)
new_version=$(cat ../../../VERSION)
echo "Publishing version $new_version"
yarn npm publish --access public
