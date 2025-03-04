#!/bin/bash -x
# publish the npm package to npmjs.org

echo "//registry.npmjs.org/:_authToken=((open_api_clients.npmjs_token))" > .npmrc
export COREPACK_ENABLE_DOWNLOAD_PROMPT=0
corepack enable
mkdir yarn-global
yarn config set globalFolder ./yarn-global
yarn install --immutable
yarn build
# OK so this is vaguely gross but it will do :)
new_version=$(cat ../../../VERSION)
echo "Publishing version $new_version"
npm publish --access public
