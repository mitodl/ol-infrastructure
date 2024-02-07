#!/bin/bash
# Commmit the updated clients w/ a commit message referencing
# the mit-open commit we generated it from.

pushd mit-open || return 1

OPEN_REV="$(git rev-parse --short HEAD)"

popd || return 2
pushd open-api-clients || return 3

# only src/ files are expected to be modified
git add src/

git status

git config --global user.name "MIT Open Learning Engineering"
git config --global user.email "oldevops@mit.edu"

git commit -m "Generated clients from rev: $OPEN_REV"
