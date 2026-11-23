#!/usr/bin/env bash
# seed-scim-tokens.sh — Create the bearer tokens Keycloak uses for outbound SCIM.
#
# Keycloak's scim-for-keycloak remote providers authenticate to each app with a
# static bearer token (configured in local-dev/infra/modules/keycloak_scim.py).
# Each app validates that token as a django-oauth-toolkit AccessToken row, so
# the same value has to exist in the app's own database. This script writes
# those rows, idempotently.
#
# Both apps gate SCIM on mitol.scim.utils.is_authenticated_predicate, which
# requires the token's owning user to be active AND staff. mit-learn
# additionally requires the `scim` OAuth2 scope (main/middleware/scim_scope.py);
# mitxonline does not check scope.
#
# The token belongs to a dedicated service user this script creates, not to
# admin@ol.test. Nothing creates an admin@ol.test row in mit-learn — its
# bootstrap only migrates and loads fixtures, and real users appear when
# ApisixUserMiddleware sees them on a first browser login — so depending on one
# would mean this never ran unattended. A row pre-created under a human's email
# would also sit in the way of that login path, which matches on global_id and
# email. A service user no one logs in as avoids both problems, and it makes the
# token's owner obvious in the admin.
#
# Usage:
#   ./local-dev/scripts/seed-scim-tokens.sh [--app <name>]
#
# Apps whose deployment is absent are skipped, so this works on a stack running
# only one of them. It fails only if no app was seeded at all.
#
# Token values default to the same literals the Pulumi stack defaults to.
# Override both sides together, e.g.:
#   pulumi config set --stack local-dev.apps-infra.Dev learn_scim_token <value>
#   LEARN_SCIM_TOKEN=<value> ./local-dev/scripts/seed-scim-tokens.sh

set -euo pipefail

LEARN_SCIM_TOKEN="${LEARN_SCIM_TOKEN:-local-dev-learn-scim-token}"  # pragma: allowlist secret
MITXONLINE_SCIM_TOKEN="${MITXONLINE_SCIM_TOKEN:-local-dev-mitxonline-scim-token}"  # pragma: allowlist secret
# Both apps use username as USERNAME_FIELD; mitxonline additionally requires a
# non-blank email.
SERVICE_USERNAME="scim-service@ol.test"
ONLY_APP=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --app) ONLY_APP="$2"; shift 2 ;;
        -h|--help) sed -n '2,33p' -- "${BASH_SOURCE[0]}"; exit 0 ;;
        *) echo "Unknown argument: $1" >&2; exit 1 ;;
    esac
done

log()  { printf '\033[1;34m==>\033[0m %s\n' "$1"; }
ok()   { printf '\033[1;32m  ✓\033[0m %s\n' "$1"; }
skip() { printf '\033[1;33m  -\033[0m %s\n' "$1"; }
err()  { printf '\033[1;31m  ✗\033[0m %s\n' "$1" >&2; }

# Written as one statement per line so a failure points at a real line number.
# update_or_create keyed on the token value: re-running with the same value
# refreshes the expiry instead of stacking duplicate rows, and the unique
# constraint on token would reject a plain create.
scim_token_program() {
    local token="$1" scope="$2"
    cat <<PY
import datetime
from django.contrib.auth import get_user_model
from django.utils import timezone
from oauth2_provider.models import AccessToken

User = get_user_model()
owner, created_user = User.objects.get_or_create(
    username="${SERVICE_USERNAME}",
    defaults={"email": "${SERVICE_USERNAME}"},
)
owner.is_staff = True
owner.is_active = True
owner.set_unusable_password()
owner.save()
print("created" if created_user else "reused", "service user", owner.username)
token, created = AccessToken.objects.update_or_create(
    token="${token}",
    defaults={
        "user": owner,
        "scope": "${scope}",
        "expires": timezone.now() + datetime.timedelta(days=365),
    },
)
print("created" if created else "refreshed", "token for", owner.username, "scope=${scope}")
PY
}

# Namespace/deployment pairs, mirroring APP_NAMESPACE and APP_DEPLOY in seed.sh.
# Not delegated to seed.sh: it execs without -i, so a program piped on stdin
# never reaches the pod, and the alternative (manage.py shell -c '<program>')
# would squeeze this multi-statement program through two layers of shell quoting.
declare -A APP_NAMESPACE=(
    [mit-learn]="mit-learn"
    [mitxonline]="mitxonline"
)
declare -A APP_DEPLOY=(
    [mit-learn]="mitlearn-webapp"
    [mitxonline]="mitxonline-app"
)
declare -A APP_TOKEN=(
    [mit-learn]="${LEARN_SCIM_TOKEN}"
    [mitxonline]="${MITXONLINE_SCIM_TOKEN}"
)

seeded=0
failed=0

seed_app() {
    local app="$1"
    local ns="${APP_NAMESPACE[$app]}" deploy="${APP_DEPLOY[$app]}"

    # A stack can run either app on its own, so an absent deployment is a skip
    # rather than an error. Returning non-zero here would abort the whole run
    # under set -e and leave the other app unseeded.
    if ! kubectl --context local-dev get deploy "${deploy}" -n "${ns}" >/dev/null 2>&1; then
        skip "${app}: no ${deploy} in ${ns}, skipping."
        return 0
    fi

    log "Seeding the ${app} SCIM token..."
    # mit-learn needs the scope to be exactly "scim": SCIMScopeCheckMiddleware
    # calls token.is_valid(["scim"]) and 401s otherwise. mitxonline ignores
    # scope, so one token shape works for both.
    if scim_token_program "${APP_TOKEN[$app]}" scim \
        | kubectl --context local-dev exec -i -n "${ns}" "deploy/${deploy}" \
            -- python manage.py shell; then
        ok "${app} SCIM token seeded."
        seeded=$((seeded + 1))
    else
        err "${app}: seeding failed. Is the pod up and migrated?"
        failed=$((failed + 1))
    fi
}

for app in mit-learn mitxonline; do
    if [[ -n "${ONLY_APP}" && "${ONLY_APP}" != "${app}" ]]; then
        continue
    fi
    seed_app "${app}"
done

if (( failed > 0 )); then
    err "${failed} app(s) failed to seed."
    exit 1
fi

if (( seeded == 0 )); then
    err "No app was seeded: neither deployment is running."
    err "Enable mit-learn or mitxonline in tilt_config.json and retry."
    exit 1
fi

ok "SCIM tokens seeded for ${seeded} app(s). They must match the apps-infra stack's token config."
