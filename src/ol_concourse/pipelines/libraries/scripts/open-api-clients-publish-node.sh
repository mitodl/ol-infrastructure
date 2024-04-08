#!/bin/bash -x
# publish the npm package to npmjs.org

yarn install --immutable
yarn build
npm publish --access public
