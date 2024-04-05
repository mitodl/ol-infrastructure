#!/bin/bash
# publish the npm package to npmjs.org

yarn build
npm publish --access public
