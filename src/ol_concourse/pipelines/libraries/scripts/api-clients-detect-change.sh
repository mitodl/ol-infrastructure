#!/bin/sh
# Decide whether this generation changed the client, before anything sets a
# version. Only a changed client is versioned, committed and published: a spec
# edit or a release that leaves the generated sources as they were publishes
# nothing.
#
# Tracked files only, the set api-clients-commit-changes.sh stages with
# `git add -u`. Leaves two files under .git/, which nothing commits:
#   client-changed  present only when the client changed; the version and
#                   commit steps do nothing without it.
#   publish-tag     the version to tag the client repo with, read by the put.
#                   Empty here; the version step fills it. The put creates no
#                   tag from an empty file, and with no new commit it pushes
#                   nothing.
set -eu

: >.git/publish-tag
rm -f .git/client-changed
changes="$(git status --porcelain --untracked-files=no -- src)"
if [ -n "$changes" ]; then
	echo "$changes"
	touch .git/client-changed
	echo "The generated client changed: it will be versioned, committed and published."
else
	echo "The generated client is unchanged: nothing will be versioned, committed or published."
fi
