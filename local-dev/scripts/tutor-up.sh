#!/usr/bin/env bash
#
# Run the host's `tutor dev` instance under Tilt.
#
# Used as the serve_cmd of the openedx-tutor resource, so its output is the
# LMS/CMS log stream in the Tilt UI and stopping Tilt stops the platform.
#
# First run per platform does a full `tutor dev launch`: it builds the
# openedx-dev image if it is missing (slow — tens of minutes), starts MySQL,
# MongoDB, Redis and Meilisearch, runs migrations, and creates the OAuth
# applications Studio needs. Subsequent runs skip straight to `tutor dev
# start`, which is why the launch is fingerprinted below.
set -euo pipefail

ROOT_DOMAIN="${LOCAL_DEV_ROOT_DOMAIN:-mit.dev}"
TUTOR="${TUTOR_BIN:-tutor}"
LMS_HOST="lms.${ROOT_DOMAIN}"

log() { echo "▶ $*"; }

# Runs it rather than just looking for it — see the note in tutor-configure.sh.
# Checked before anything is written, so a broken tutor cannot leave half of a
# two-sided change applied.
"${TUTOR}" --version &>/dev/null || {
    echo "  ✗ '${TUTOR}' is not a working tutor. Set TUTOR_BIN to the real one;" >&2
    echo "    ./local-dev/scripts/tutor-configure.sh explains the options." >&2
    exit 1
}

TUTOR_ROOT_DIR=$("${TUTOR}" config printroot)
# Records the hostname the platform was last initialised for. A changed
# LMS_HOST has to re-run init: the OAuth applications tutor creates there
# (notably Studio's cms-sso) bake the hostname into their redirect URIs.
LAUNCH_MARKER="${TUTOR_ROOT_DIR}/.ol-local-dev-launched"

if [[ "${OL_TUTOR_RELAUNCH:-0}" == "1" ]] \
    || [[ ! -f "${LAUNCH_MARKER}" ]] \
    || [[ "$(cat "${LAUNCH_MARKER}")" != "${LMS_HOST}" ]]; then
    log "Launching Open edX (first run for ${LMS_HOST}; this can take a while)"
    "${TUTOR}" dev launch --non-interactive
    echo "${LMS_HOST}" > "${LAUNCH_MARKER}"

    # `tutor dev launch` starts the containers *before* it migrates, so on an
    # empty database LMS and Studio boot against a schema that does not exist
    # yet. Django's system checks read a waffle switch, that raises
    # ProgrammingError("Table 'openedx.waffle_switch' doesn't exist"), and the
    # django-main-thread dies without ever binding port 8000 — while the
    # StatReloader keeps the container Up, so nothing notices. `tutor dev start`
    # below then leaves the healthy-looking container alone and the readiness
    # probe waits out its whole budget on a platform that will never answer.
    # Restarting them once the migrations are in is the whole fix.
    log "Restarting LMS and Studio against the migrated database"
    "${TUTOR}" dev restart lms cms
else
    log "Open edX already initialised for ${LMS_HOST} (re-run with OL_TUTOR_RELAUNCH=1 to redo init)"
fi

log "Starting Open edX — https://${LMS_HOST} · https://studio.${ROOT_DOMAIN}"
# Attached, so Tilt owns the process: container logs stream into the UI and
# Ctrl-C / `tilt down` stops the compose project.
exec "${TUTOR}" dev start
