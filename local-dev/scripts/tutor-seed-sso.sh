#!/usr/bin/env bash
#
# Wire Open edX login to mitxonline (SSO), the way deployed environments do it.
#
# Run automatically by the openedx-tutor-sso Tilt resource, and idempotent:
#
#     ./local-dev/scripts/tutor-seed-sso.sh
#
# The flow this sets up:
#
#   1. anonymous request to the LMS -> /auth/login/ol-oauth2/
#   2. LMS sends the browser to https://mitxonline.<domain>/oauth2/authorize/
#   3. mitxonline requires a session, so APISIX takes the browser through
#      Keycloak (the olapps realm) and back
#   4. mitxonline redirects to https://lms.<domain>/auth/complete/ol-oauth2/
#   5. the LMS exchanges the code and reads the account, both over the plain
#      HTTP internal route, and creates or links its own user
#
# Two records have to agree for that to work, so both are created here:
#
#   - mitxonline: the "edx-oauth-app" OAuth2 Application (Open edX's client).
#     The name matters — mitxonline looks the application up by
#     OPENEDX_OAUTH_APP_NAME when it provisions learners.
#   - Open edX: an enabled OAuth2ProviderConfig for the ol-oauth2 backend
#     holding the same client id and secret.
#
# Everything else Open edX needs (the backend in THIRD_PARTY_AUTH_BACKENDS, the
# forced-SSO redirect) comes from the ol_local_dev tutor plugin.
set -euo pipefail

ROOT_DOMAIN="${LOCAL_DEV_ROOT_DOMAIN:-mit.dev}"
TUTOR="${TUTOR_BIN:-tutor}"

# Open edX's credentials for mitxonline. Local-dev only, and only ever sent
# between the two containers.
CLIENT_ID="edx-oauth-app-local-dev"
CLIENT_SECRET="edx-oauth-app-local-dev-secret"  # pragma: allowlist secret

# Where mitxonline sends the browser back to. Public, TLS-terminated by APISIX.
REDIRECT_URI="https://lms.${ROOT_DOMAIN}/auth/complete/ol-oauth2/"

# Browser-facing, so it must be the public URL...
AUTHORIZATION_URL="https://mitxonline.${ROOT_DOMAIN}/oauth2/authorize/"
# ...while these two are called by the LMS container, which does not trust the
# mkcert CA. They go over plain HTTP through the internal-only route in
# local-dev/apps/mitxonline/apisix-routes-openedx.yaml.
ACCESS_TOKEN_URL="http://mitxonline-internal.${ROOT_DOMAIN}/oauth2/token/"
API_ROOT="http://mitxonline-internal.${ROOT_DOMAIN}/"

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

# Same for mitxonline: `kubectl exec deploy/...` picks whatever pod it finds,
# which during a rollout can be one that is already terminating.
wait_for_mitxonline() {
    kubectl rollout status -n mitxonline deploy/mitxonline-webapp --timeout=180s
}

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

log "Wiring Open edX SSO through mitxonline"

wait_for_lms
wait_for_mitxonline

# ---------------------------------------------------------------------------
# 1. mitxonline: the OAuth2 application Open edX authenticates as
# ---------------------------------------------------------------------------
# client_secret is stored hashed by django-oauth-toolkit; assigning the
# plaintext and saving is what hashes it, and re-running simply re-hashes the
# same value.
read -r -d '' MITXONLINE_PY <<PY || true
from oauth2_provider.models import get_application_model

Application = get_application_model()
app, created = Application.objects.get_or_create(name="edx-oauth-app")
app.client_id = "${CLIENT_ID}"
app.client_secret = "${CLIENT_SECRET}"
app.client_type = "confidential"
app.authorization_grant_type = "authorization-code"
# No consent screen: Open edX is a first-party client here.
app.skip_authorization = True
app.redirect_uris = "${REDIRECT_URI}"
app.save()
print("created" if created else "updated", "edx-oauth-app", app.client_id)
PY

# Same exposure on the cluster side: a pod that starts rolling mid-command
# takes the exec with it.
kubectl exec -n mitxonline deploy/mitxonline-webapp -c app -- \
    python manage.py shell -c "${MITXONLINE_PY}" || {
    wait_for_mitxonline
    kubectl exec -n mitxonline deploy/mitxonline-webapp -c app -- \
        python manage.py shell -c "${MITXONLINE_PY}"
}
ok "mitxonline application: ${CLIENT_ID} -> ${REDIRECT_URI}"

# ---------------------------------------------------------------------------
# 2. Open edX: the third-party auth provider
# ---------------------------------------------------------------------------
# OAuth2ProviderConfig is a ConfigurationModel: rows are append-only and the
# newest one per (site_id, backend_name) wins, so this always inserts.
#
# The site has to be the one whose *domain* is LMS_HOST. Third-party auth
# resolves the provider through Site.objects.get_current(request), and inside a
# request edx-platform answers that from the Host header — not from SITE_ID,
# which on a stock tutor install points at example.com. A provider row attached
# to any other site is invisible to the login view, which is exactly the
# "Can\'t fetch setting of a disabled backend/provider" error: the lookup finds
# no row for the request\'s site and falls back to a blank, disabled config.
# Repointing LMS_HOST therefore orphans the previous hostname\'s provider row.
read -r -d '' OPENEDX_PY <<PY || true
import json

from django.contrib.sites.models import Site
from common.djangoapps.third_party_auth.models import OAuth2ProviderConfig

site, _ = Site.objects.get_or_create(
    domain="lms.${ROOT_DOMAIN}", defaults={"name": "MITx Online local dev"}
)
desired = {
    "enabled": True,
    # Also offer it as a button on the LMS's own login page, so SSO still works
    # with the forced redirect turned off.
    "visible": True,
    "name": "MITx Online",
    "slug": "ol-oauth2",
    "backend_name": "ol-oauth2",
    "key": "${CLIENT_ID}",
    "secret": "${CLIENT_SECRET}",
    "other_settings": json.dumps(
        {
            "AUTHORIZATION_URL": "${AUTHORIZATION_URL}",
            "ACCESS_TOKEN_URL": "${ACCESS_TOKEN_URL}",
            "API_ROOT": "${API_ROOT}",
        },
        indent=4,
    ),
    # mitxonline already collected the profile and verified the address, and it
    # stays the source of truth for both.
    "skip_registration_form": True,
    "skip_email_verification": True,
    "skip_hinted_login_dialog": True,
    "sync_learner_profile_data": True,
    "icon_class": "fa-sign-in",
}

# Rows are append-only, so an unconditional create would add one on every Tilt
# build. Compare against the newest row first and only insert when something
# actually differs; other_settings is compared parsed, not as text, so
# reformatting it here does not count as a change.
current = (
    OAuth2ProviderConfig.objects.filter(site=site, backend_name="ol-oauth2")
    .order_by("-change_date", "-id")
    .first()
)


def differs(row):
    if row is None:
        return True
    for field, value in desired.items():
        have = getattr(row, field)
        if field == "other_settings":
            if json.loads(have or "{}") != json.loads(value):
                return True
        elif have != value:
            return True
    return False


if differs(current):
    config = OAuth2ProviderConfig.objects.create(site=site, **desired)
    print("provider config", config.id, "site", site.id, site.domain, "created")
else:
    print("provider config", current.id, "site", site.id, site.domain, "unchanged")
PY

lms ./manage.py lms shell -c "${OPENEDX_PY}"
ok "Open edX provider: ol-oauth2 -> ${AUTHORIZATION_URL}"

log "Done. Logging in at https://lms.${ROOT_DOMAIN} now goes through mitxonline."
