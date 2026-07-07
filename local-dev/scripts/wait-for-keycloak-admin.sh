#!/usr/bin/env bash
# wait-for-keycloak-admin.sh — Block until Keycloak's admin REST API is ready.
#
# Why this exists:
#   Keycloak serves its OIDC *discovery* endpoint
#   (/realms/master/.well-known/openid-configuration) noticeably earlier than
#   its *admin* REST API can reliably service write operations such as realm
#   and client creation. Gating `pulumi up` on discovery alone races the
#   pulumi-keycloak provider into the warm-up window, where POST /admin/realms
#   times out ("context deadline exceeded"). A timed-out realm-create still
#   succeeds server-side but is never recorded in Pulumi state, leaving the
#   stack wedged on 409 "Realm already exists" on every retry.
#
#   This gate instead proves the admin API is actually serving: it obtains an
#   admin token AND performs an authenticated admin read (GET /admin/realms)
#   before returning success.
#
# Usage:
#   wait-for-keycloak-admin.sh <keycloak-host> [attempts] [sleep-seconds]
#
# Examples:
#   wait-for-keycloak-admin.sh sso.ol.mit.dev          # 60 attempts, 5s apart
#   KEYCLOAK_ADMIN_PASSWORD=wrong \
#     wait-for-keycloak-admin.sh sso.ol.mit.dev 2 1    # negative-path test
#
# Admin credentials default to the local-dev admin/admin used by the
# pulumi-keycloak provider; override via KEYCLOAK_ADMIN_USER / _PASSWORD.
set -euo pipefail

HOST="${1:?usage: wait-for-keycloak-admin.sh <keycloak-host> [attempts] [sleep]}"
ATTEMPTS="${2:-60}"
SLEEP="${3:-5}"
ADMIN_USER="${KEYCLOAK_ADMIN_USER:-admin}"
ADMIN_PASS="${KEYCLOAK_ADMIN_PASSWORD:-admin}"

token_url="https://${HOST}/realms/master/protocol/openid-connect/token"
admin_url="https://${HOST}/admin/realms"

SECONDS=0  # bash elapsed-time counter; tracks real wall-clock, curl time included
echo "Waiting for Keycloak admin API at ${HOST} (up to $((ATTEMPTS * SLEEP))s)..."
for _ in $(seq 1 "${ATTEMPTS}"); do
    token=$(curl -sfk --max-time 10 \
        --data "client_id=admin-cli&grant_type=password&username=${ADMIN_USER}&password=${ADMIN_PASS}" \
        "${token_url}" 2>/dev/null \
        | sed -n 's/.*"access_token":"\([^"]*\)".*/\1/p') || true

    if [ -n "${token}" ] \
        && curl -sfk --max-time 10 -o /dev/null -H "Authorization: Bearer ${token}" "${admin_url}"; then
        echo "Keycloak admin API ready after ${SECONDS}s."
        exit 0
    fi
    sleep "${SLEEP}"
done

echo "ERROR: Keycloak admin API not ready after ${SECONDS}s." >&2
exit 1
