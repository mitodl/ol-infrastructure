#!/bin/bash
# Bump the client version with the clients repo's bumpver config, for a source
# repo whose releases are not CalVer tags (mit-learn, until it moves to the
# release-resource workflow). A source on release tags takes its version from
# the tag instead: see api-clients-version-from-tag.py.
#
# Only when api-clients-detect-change.sh found a changed client. The new
# version goes to .git/publish-tag, which the put tags the commit with.
set -e

if [ ! -e .git/client-changed ]; then
	echo "The client is unchanged: not bumping the version."
	exit 0
fi

pip install bumpver

bumpver update

cp VERSION .git/publish-tag
