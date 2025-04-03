#!/bin/bash
# Commit the updated version and tag it

VERSION=$(cat VERSION)

git add .
git status

git config --global user.name "MIT Open Learning Engineering"
git config --global user.email "oldevops@mit.edu"

git commit -m "Release: $VERSION"

git tag "v$VERSION"
