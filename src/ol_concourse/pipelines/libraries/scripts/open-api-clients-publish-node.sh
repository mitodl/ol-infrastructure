#!/bin/bash
# publish the npm package to npmjs.org

yarn install --immutable
yarn build
npm publish --access public
