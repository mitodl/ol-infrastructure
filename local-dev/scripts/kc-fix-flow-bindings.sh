#!/usr/bin/env bash
# kc-fix-flow-bindings.sh — Re-assert the olapps realm's browser/first-broker-login
# flow bindings after every apply.
#
# keycloak.py binds browserFlow/firstBrokerLoginFlow to our custom "Organization
# browser"/"Organization first broker login" flows via a keycloak.authentication.Bindings
# resource. Any later update to the keycloak.Realm resource itself (e.g. changing
# smtp_server, password_policy, etc.) PUTs a full realm representation whose flow
# fields come from the Realm's own Pulumi state. That state only changes on a
# refresh, and after creation it holds the built-in defaults ("browser" / "first
# broker login"), so without a refresh the PUT rebinds them. Pulumi never notices, because Bindings' own inputs didn't change,
# so it doesn't reapply. Without the custom "Organization browser" flow,
# the login page's identity-first screen dead-ends at a password-reset prompt
# instead of routing unrecognized emails to registration.
#
# Usage:
#   ./local-dev/scripts/kc-fix-flow-bindings.sh
#
# kc-admin.sh resolves the Keycloak URL.

set -euo pipefail

# shellcheck source=local-dev/scripts/kc-admin.sh disable=SC1091  # followed only with -x
source "$(dirname "${BASH_SOURCE[0]}")/kc-admin.sh"

echo "[kc-fix-flow-bindings] Connecting to Keycloak at ${KC_URL} ..."
TOKEN=$(get_token)

CURRENT=$(curl -sf --cacert "${KC_CACERT}" --max-time 10 \
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
curl -sf --cacert "${KC_CACERT}" --max-time 10 \
    -X PUT \
    -H "Authorization: Bearer ${TOKEN}" \
    -H "Content-Type: application/json" \
    "${KC_URL}/admin/realms/${REALM}" \
    -d "$PATCHED" \
    -o /dev/null

echo "[kc-fix-flow-bindings] Done."
