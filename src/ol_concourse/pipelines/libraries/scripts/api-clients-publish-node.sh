#!/bin/bash -x
# publish the npm package to npmjs.org

#echo "//registry.npmjs.org/:_authToken=$NPM_TOKEN" > .npmrc
echo "npmPublishRegistry: https://registry.npmjs.org/" > .yarnrc.yml
echo "npmAuthToken: $NPM_TOKEN" >> .yarnrc.yml
corepack enable
COREPACK_ENABLE_DOWNLOAD_PROMPT=0 yarn install --immutable
yarn build
# OK so this is vaguely gross but it will do :)
new_version=$(cat ../../../VERSION)
echo "Publishing version $new_version"
yarn npm publish --access public
