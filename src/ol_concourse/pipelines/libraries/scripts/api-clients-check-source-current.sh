#!/bin/sh
# Decide whether a prerelease build's specs are still the ones on the source
# branch, before api-clients-publish-node-prerelease.sh writes to npm.
#
# The job is serial, so builds publish in trigger order, but a re-run (or a
# pinned version) replays an older build's inputs after newer builds have
# published. Its client would then go out as the newest `next`, or move `next`
# back to `latest`, with an API main no longer has. So compare this build's
# specs with the live branch head's: when they differ, this build is stale and
# a newer one (already run, or queued behind this one) owns `next`.
#
# Specs, not commit IDs: the resource watches openapi/specs only, so the head
# may be a later commit that left the specs as they were.
#
# Runs in the client repo's checkout and leaves .git/source-stale there, which
# nothing commits, only when the build is stale. A failed fetch fails the
# build rather than publishing from a build it could not check.
set -eu

rm -f .git/source-stale
git -C "$SOURCE_DIR" fetch --quiet "$SOURCE_URI" "refs/heads/$SOURCE_BRANCH"
if git -C "$SOURCE_DIR" diff --quiet HEAD FETCH_HEAD -- openapi/specs; then
	echo "This build's specs are the ones on $SOURCE_BRANCH: it may publish."
else
	git -C "$SOURCE_DIR" diff --stat HEAD FETCH_HEAD -- openapi/specs
	touch .git/source-stale
	echo "$SOURCE_BRANCH's specs have moved on since this build: it publishes nothing."
fi
