#!/usr/bin/env bash
# pulumi-clear-stale-lock.sh <lock-dir>
#
# Remove a Pulumi file-backend lock left by a run of ours that no longer
# exists, so the next `pulumi up` does not fail with "the stack is currently
# locked". Only that: a lock whose PID is alive is a real run, a lock from
# another user is not ours to judge, and the directory stays. Never `pulumi
# cancel`, which would abort a live run.
set -euo pipefail
lock_dir="${1:?usage: $0 <lock-dir>}"
[ -d "$lock_dir" ] || exit 0
for lock in "$lock_dir"/*.json; do
    [ -e "$lock" ] || continue
    read -r pid user < <(python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print(d["pid"], d["username"])' "$lock")
    if [ "$user" = "$(id -un)" ] && ! kill -0 "$pid" 2>/dev/null; then
        echo "removing stale pulumi lock $(basename "$lock") (pid $pid is gone)" >&2
        rm -f "$lock" "$lock.attrs"
    fi
done
