#!/usr/bin/env bash
# pulumi-up-retry.sh <attempts> <lock-dir> <pulumi up args...>
#
# Runs `pulumi up` until it succeeds, at most <attempts> times. Tilt does not
# re-run a failed local_resource on its own, so one failed apply holds back
# every resource that depends on it until someone retriggers it by hand.
#
# It exists for local-infra-apps' first run on a fresh cluster: the olapps
# realm create can time out while Keycloak still applies it, and the next run
# adopts that realm (live_realm_import_id in local-dev/infra/modules/keycloak.py).
# A genuine error fails every attempt, each with its own output, and the
# resource still goes red after the last one.
#
# A run that dies without releasing its file-backend lock (killed, OOM, the
# machine sleeping mid-apply) would otherwise fail every later attempt with
# "the stack is currently locked", so a lock whose process is gone is cleared
# before each attempt. A live run's lock is left alone.
set -uo pipefail
# Pulumi handles Ctrl-C itself and exits non-zero, which would otherwise read
# as a failed attempt and start another apply the user just cancelled.
trap 'exit 130' INT
trap 'exit 143' TERM
usage="usage: pulumi-up-retry.sh <attempts> <lock-dir> <pulumi up args...>"
attempts="${1:?$usage}"
lock_dir="${2:?$usage}"
shift 2
clear_stale_lock="$(dirname "${BASH_SOURCE[0]}")/pulumi-clear-stale-lock.sh"
for attempt in $(seq 1 "${attempts}"); do
    "${clear_stale_lock}" "${lock_dir}"
    pulumi up "$@" && exit 0
    status=$?
    echo "pulumi up failed (exit ${status}) on attempt ${attempt} of ${attempts}" >&2
done
exit "${status}"
