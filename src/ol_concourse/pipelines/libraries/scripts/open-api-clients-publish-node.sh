#!/bin/bash
# publish the npm package to npmjs.org

yarn add tsc
yarn build
npm publish --access public
