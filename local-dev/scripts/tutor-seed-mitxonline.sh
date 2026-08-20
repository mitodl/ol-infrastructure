#!/usr/bin/env bash
#
# Give mitxonline API access to the local Open edX.
#
# Run automatically by the openedx-tutor-seed Tilt resource once the platform
# is up, and idempotent, so it is safe to re-run:
#
#     ./local-dev/scripts/tutor-seed-mitxonline.sh
#
# It creates, inside the LMS:
#   - a staff/superuser service-worker account and a long-lived access token
#     for it (mitxonline's OPENEDX_SERVICE_WORKER_API_TOKEN)
#   - an authorization-code OAuth2 application, used for the per-learner tokens
#     mitxonline exchanges after a user links their Open edX account
#   - a client-credentials OAuth2 application for the course-sync calls
#
# The credentials below are the *other* half of
# local-dev/apps/mitxonline/configmaps/app-env-openedx.yaml — change them in
# both places or the integration silently 401s.
set -euo pipefail

ROOT_DOMAIN="${LOCAL_DEV_ROOT_DOMAIN:-mit.dev}"
TUTOR="${TUTOR_BIN:-tutor}"

SERVICE_WORKER_USERNAME="mitxonline_service_worker"
SERVICE_WORKER_EMAIL="mitxonline-service-worker@${ROOT_DOMAIN}"
SERVICE_WORKER_TOKEN="mitxonline-local-dev-service-worker-token"  # pragma: allowlist secret

API_CLIENT_ID="mitxonline-local-dev"
API_CLIENT_SECRET="mitxonline-local-dev-secret"  # pragma: allowlist secret
COURSES_CLIENT_ID="mitxonline-courses-local-dev"
COURSES_CLIENT_SECRET="mitxonline-courses-local-dev-secret"  # pragma: allowlist secret

# mitxonline sends the learner here after Open edX authorises it
# (openedx/urls.py: login/_private/complete).
REDIRECT_URI="https://mitxonline.${ROOT_DOMAIN}/login/_private/complete"
SCOPES="read,write,email,profile"

log() { echo "▶ $*"; }
ok()  { echo "  ✓ $*"; }

# Runs it rather than just looking for it — see the note in tutor-configure.sh.
# Checked before anything is written, so a broken tutor cannot leave half of a
# two-sided change applied.
"${TUTOR}" --version &>/dev/null || {
    echo "  ✗ '${TUTOR}' is not a working tutor. Set TUTOR_BIN to the real one;" >&2
    echo "    ./local-dev/scripts/tutor-configure.sh explains the options." >&2
    exit 1
}

# -T (inside the helper): no pseudo-TTY. Tilt runs this without one, and docker
# compose exec otherwise fails with "the input device is not a TTY".
# `docker compose exec` is killed outright — status 137 — when its container
# goes away mid-command: a `tutor dev restart`, a re-render of the settings, or
# Tilt reconciling the openedx-tutor resource. Everything below is idempotent,
# so retry rather than failing the whole resource. A command that fails while
# exec still works failed on its own merits and is reported immediately.
lms() {
    local attempt=1
    while true; do
        if "${TUTOR}" dev exec -T lms "$@"; then
            return 0
        fi
        if "${TUTOR}" dev exec -T lms true >/dev/null 2>&1; then
            return 1
        fi
        if (( attempt >= 3 )); then
            echo "  ✗ the LMS container kept going away; giving up." >&2
            return 1
        fi
        echo "  … the LMS container went away mid-command, retrying"
        wait_for_lms
        attempt=$((attempt + 1))
    done
}

# The container can be up one moment and restarting the next — a config change,
# a `tutor dev restart`, Tilt reconciling the openedx-tutor resource — and
# `tutor dev exec` fails outright when it is. Waiting here turns a red resource
# into a short pause, since this script is safe to run the moment exec works.
wait_for_lms() {
    local waited=0
    until "${TUTOR}" dev exec -T lms true >/dev/null 2>&1; do
        if (( waited >= 180 )); then
            echo "  ✗ the LMS container has not accepted an exec in 3 minutes." >&2
            echo "    Check the openedx-tutor resource, then re-run this script." >&2
            exit 1
        fi
        [[ ${waited} -eq 0 ]] && echo "  … waiting for the LMS container"
        sleep 3
        waited=$((waited + 3))
    done
}

log "Seeding Open edX for mitxonline"

wait_for_lms

lms ./manage.py lms manage_user \
    "${SERVICE_WORKER_USERNAME}" "${SERVICE_WORKER_EMAIL}" \
    --staff --superuser --unusable-password
ok "service worker: ${SERVICE_WORKER_USERNAME}"

lms ./manage.py lms create_dot_application \
    --grant-type authorization-code \
    --redirect-uris "${REDIRECT_URI}" \
    --client-id "${API_CLIENT_ID}" \
    --client-secret "${API_CLIENT_SECRET}" \
    --scopes "${SCOPES}" \
    --skip-authorization \
    --update \
    mitxonline "${SERVICE_WORKER_USERNAME}"
ok "authorization-code application: ${API_CLIENT_ID}"

lms ./manage.py lms create_dot_application \
    --grant-type client-credentials \
    --client-id "${COURSES_CLIENT_ID}" \
    --client-secret "${COURSES_CLIENT_SECRET}" \
    --scopes "${SCOPES}" \
    --update \
    mitxonline-courses "${SERVICE_WORKER_USERNAME}"
ok "client-credentials application: ${COURSES_CLIENT_ID}"

# create_dot_application cannot mint a token, and mitxonline expects a fixed
# one it can read from its environment rather than a value discovered at
# runtime, so the token row is written directly.
read -r -d '' TOKEN_PY <<PY || true
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.utils import timezone
from oauth2_provider.models import get_access_token_model, get_application_model

user = get_user_model().objects.get(username="${SERVICE_WORKER_USERNAME}")
application = get_application_model().objects.get(client_id="${API_CLIENT_ID}")
token, created = get_access_token_model().objects.update_or_create(
    token="${SERVICE_WORKER_TOKEN}",
    defaults={
        "user": user,
        "application": application,
        "expires": timezone.now() + timedelta(days=365),
        "scope": "${SCOPES}".replace(",", " "),
    },
)
print("created" if created else "refreshed", "service worker token")
PY

lms ./manage.py lms shell -c "${TOKEN_PY}"
ok "service worker token valid for 365 days"

log "Done. mitxonline talks to https://lms.${ROOT_DOMAIN}"
