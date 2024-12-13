#!/bin/bash -x
# publish the npm package to npmjs.org

echo "//registry.npmjs.org/:_authToken=((npm_publish.npmjs_token))" > .npmrc
yarn install --immutable
yarn build
# OK so this is vaguely gross but it will do :)
new_version=$(cat ../../../VERSION)
echo "Publishing version $new_version"
yarn publish --access public --new-version "$new_version"
