#!/usr/bin/env bash
# kc-fix-flow-bindings.sh — Re-assert the olapps realm's browser/first-broker-login
# flow bindings after every apply.
#
# keycloak.py binds browserFlow/firstBrokerLoginFlow to our custom "Organization
# browser"/"Organization first broker login" flows via a keycloak.authentication.Bindings
# resource. That binding only gets written once: any later update to the
# keycloak.Realm resource itself (e.g. changing smtp_server, password_policy, etc.)
# PUTs a full realm representation that doesn't include those two fields, and
# Keycloak's API resets them to its built-in defaults ("browser" / "first broker
# login") as a result. Pulumi never notices, because Bindings' own inputs didn't
# change, so it doesn't reapply. Without the custom "Organization browser" flow,
# the login page's identity-first screen dead-ends at a password-reset prompt
# instead of routing unrecognized emails to registration.
#
# Usage:
#   ./local-dev/scripts/kc-fix-flow-bindings.sh
#
# Resolves the Keycloak URL the same way kc-seed-users.sh does.

set -euo pipefail

_ROOT_DOMAIN="${LOCAL_DEV_ROOT_DOMAIN:-mit.dev}"
KC_URL="${KC_URL:-https://sso.ol.${_ROOT_DOMAIN}}"
REALM="olapps"
KC_USER="admin"
KC_PASS="admin"  # pragma: allowlist secret

get_token() {
    local token attempt
    for attempt in 1 2 3 4 5; do
        token=$(curl -sf --max-time 10 \
            -X POST "${KC_URL}/realms/master/protocol/openid-connect/token" \
            -d "client_id=admin-cli" \
            -d "grant_type=password" \
            -d "username=${KC_USER}" \
            -d "password=${KC_PASS}" 2>/dev/null \
            | jq -r '.access_token // empty' 2>/dev/null || true)
        if [ -n "$token" ] && [ "$token" != "null" ]; then
            echo "$token"
            return 0
        fi
        echo "[kc-fix-flow-bindings] attempt ${attempt}/5: waiting for Keycloak..." >&2
        sleep 5
    done
    echo "[kc-fix-flow-bindings] ERROR: could not obtain admin token from ${KC_URL}" >&2
    return 1
}

echo "[kc-fix-flow-bindings] Connecting to Keycloak at ${KC_URL} ..."
TOKEN=$(get_token)

CURRENT=$(curl -sf --max-time 10 \
    -H "Authorization: Bearer ${TOKEN}" \
    "${KC_URL}/admin/realms/${REALM}" 2>/dev/null)

BROWSER_FLOW=$(echo "$CURRENT" | jq -r '.browserFlow // empty')
FIRST_BROKER_FLOW=$(echo "$CURRENT" | jq -r '.firstBrokerLoginFlow // empty')

if [ "$BROWSER_FLOW" = "Organization browser" ] && [ "$FIRST_BROKER_FLOW" = "Organization first broker login" ]; then
    echo "[kc-fix-flow-bindings] Flow bindings already correct, skipping."
    exit 0
fi

echo "[kc-fix-flow-bindings] Flow bindings drifted (browserFlow=${BROWSER_FLOW}, firstBrokerLoginFlow=${FIRST_BROKER_FLOW}); re-asserting ..."
PATCHED=$(echo "$CURRENT" | jq '.browserFlow = "Organization browser" | .firstBrokerLoginFlow = "Organization first broker login"')
curl -sf --max-time 10 \
    -X PUT \
    -H "Authorization: Bearer ${TOKEN}" \
    -H "Content-Type: application/json" \
    "${KC_URL}/admin/realms/${REALM}" \
    -d "$PATCHED" \
    -o /dev/null

echo "[kc-fix-flow-bindings] Done."
