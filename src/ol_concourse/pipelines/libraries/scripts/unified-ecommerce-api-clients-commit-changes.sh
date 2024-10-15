#!/bin/bash
# Commmit the updated clients w/ a commit message referencing
# the unified-ecommerce commit we generated it from.

# only src/ files are expected to be modified
git add -u

git status

git config --global user.name "MIT Open Learning Engineering"
git config --global user.email "odl-devops@mit.edu"

git commit -m "Generated clients from rev: $OPEN_REV"
