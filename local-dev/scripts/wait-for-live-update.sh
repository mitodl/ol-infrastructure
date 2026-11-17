#!/bin/sh
# Runs a command once <marker> exists, or after <seconds> without it:
#   wait-for-live-update.sh <marker> <seconds> <command...>
# Containers run this text through `sh -c` (live_update_wait in tiltlib.star).
marker=$1
tries=$(($2 * 4))
shift 2
i=0
until [ -e "$marker" ] || [ "$i" -ge "$tries" ]; do
    sleep 0.25
    i=$((i + 1))
done
[ -e "$marker" ] || echo "No copy from Tilt after $((tries / 4)) s; starting on the image's files" >&2
exec "$@"
