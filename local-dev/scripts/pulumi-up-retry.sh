#!/usr/bin/env bash
# pulumi-up-retry.sh <attempts> <pulumi up args...>
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
set -uo pipefail
# Pulumi handles Ctrl-C itself and exits non-zero, which would otherwise read
# as a failed attempt and start another apply the user just cancelled.
trap 'exit 130' INT
trap 'exit 143' TERM
attempts="${1:?usage: pulumi-up-retry.sh <attempts> <pulumi up args...>}"
shift
for attempt in $(seq 1 "${attempts}"); do
    pulumi up "$@" && exit 0
    status=$?
    echo "pulumi up failed (exit ${status}) on attempt ${attempt} of ${attempts}" >&2
done
exit "${status}"
