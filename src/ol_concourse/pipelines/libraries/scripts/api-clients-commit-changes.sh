#!/bin/bash
# Commmit the updated clients w/ a commit message referencing
# the mit-open commit we generated it from.
#
# Only when api-clients-detect-change.sh found a changed client. Otherwise
# the put pushes the unchanged HEAD, which is a no-op, and tags nothing.
if [ ! -e .git/client-changed ]; then
	echo "The client is unchanged: nothing to commit."
	exit 0
fi

# only src/ files are expected to be modified
git add -u

git status

git config --global user.name "MIT Open Learning Engineering"
git config --global user.email "odl-devops@mit.edu"

git commit -m "Generated clients for $SOURCE_REPO_NAME from rev: $OPEN_REV"
