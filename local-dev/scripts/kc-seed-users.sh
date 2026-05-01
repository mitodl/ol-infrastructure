#!/usr/bin/env bash
# kc-seed-users.sh — Idempotent local-dev test-user provisioning
#
# Creates three test users in the Keycloak olapps realm.  If a user already
# exists (by username) it is skipped — safe to run multiple times.
#
# Users created:
#   admin   / localdev123  — realm admin role
#   student / localdev123
#   prof    / localdev123
#
# Usage:
#   ./local-dev/scripts/kc-seed-users.sh
#
# The script resolves the Keycloak URL automatically (uses sso.ol.{ROOT_DOMAIN} by
# default, where ROOT_DOMAIN is read from LOCAL_DEV_ROOT_DOMAIN env var).
# Override with KC_URL env var if needed.

set -euo pipefail

_ROOT_DOMAIN="${LOCAL_DEV_ROOT_DOMAIN:-mit.dev}"
KC_URL="${KC_URL:-https://sso.ol.${_ROOT_DOMAIN}}"
REALM="olapps"
KC_USER="admin"
KC_PASS="admin"  # pragma: allowlist secret

# ---------------------------------------------------------------------------
# Helper: obtain an admin access token, retrying up to 5 times.
# ---------------------------------------------------------------------------
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
        echo "[kc-seed-users] attempt ${attempt}/5: waiting for Keycloak..." >&2
        sleep 5
    done
    echo "[kc-seed-users] ERROR: could not obtain admin token from ${KC_URL}" >&2
    return 1
}

echo "[kc-seed-users] Connecting to Keycloak at ${KC_URL} ..."
TOKEN=$(get_token)

# ---------------------------------------------------------------------------
# Ensure the 'admin' realm role exists in olapps.
# ---------------------------------------------------------------------------
ensure_admin_role() {
    local existing
    existing=$(curl -sf --max-time 10 \
        -H "Authorization: Bearer ${TOKEN}" \
        "${KC_URL}/admin/realms/${REALM}/roles/admin" 2>/dev/null \
        | jq -r '.name // empty' 2>/dev/null || true)
    if [ -z "$existing" ]; then
        echo "[kc-seed-users] Creating 'admin' realm role in ${REALM} ..."
        curl -sf --max-time 10 \
            -X POST \
            -H "Authorization: Bearer ${TOKEN}" \
            -H "Content-Type: application/json" \
            "${KC_URL}/admin/realms/${REALM}/roles" \
            -d '{"name":"admin","description":"Local dev admin role"}' \
            -o /dev/null
    fi
}

ensure_admin_role

# ---------------------------------------------------------------------------
# User definitions: (username, email, firstName, lastName, assign_admin_role)
# ---------------------------------------------------------------------------
declare -A USER_EMAILS=( [admin]="admin@odl.local" [student]="student@odl.local" [prof]="prof@odl.local" )
declare -A USER_FIRST=( [admin]="Admin" [student]="Student" [prof]="Professor" )
declare -A USER_LAST=( [admin]="User" [student]="User" [prof]="User" )
ADMIN_USERS=("admin")

for USERNAME in admin student prof; do
    EMAIL="${USER_EMAILS[$USERNAME]}"
    FIRST="${USER_FIRST[$USERNAME]}"
    LAST="${USER_LAST[$USERNAME]}"

    # Check if user already exists.
    EXISTING_ID=$(curl -sf --max-time 10 \
        -H "Authorization: Bearer ${TOKEN}" \
        "${KC_URL}/admin/realms/${REALM}/users?username=${USERNAME}&exact=true" 2>/dev/null \
        | jq -r '.[0].id // empty' 2>/dev/null || true)

    if [ -n "${EXISTING_ID}" ]; then
        echo "[kc-seed-users] ${USERNAME}: already exists (${EXISTING_ID}), skipping."
        continue
    fi

    echo "[kc-seed-users] Creating user: ${USERNAME} <${EMAIL}> ..."
    curl -sf --max-time 10 \
        -X POST \
        -H "Authorization: Bearer ${TOKEN}" \
        -H "Content-Type: application/json" \
        "${KC_URL}/admin/realms/${REALM}/users" \
        -d "{
            \"username\": \"${USERNAME}\",
            \"email\": \"${EMAIL}\",
            \"firstName\": \"${FIRST}\",
            \"lastName\": \"${LAST}\",
            \"enabled\": true,
            \"emailVerified\": true,
            \"credentials\": [{
                \"type\": \"password\",
                \"value\": \"localdev123\",
                \"temporary\": false
            }]
        }" \
        -o /dev/null

    # Re-fetch the new user ID for role assignment.
    NEW_ID=$(curl -sf --max-time 10 \
        -H "Authorization: Bearer ${TOKEN}" \
        "${KC_URL}/admin/realms/${REALM}/users?username=${USERNAME}&exact=true" 2>/dev/null \
        | jq -r '.[0].id // empty' 2>/dev/null || true)

    # Assign the admin realm role if applicable.
    for admin_user in "${ADMIN_USERS[@]}"; do
        if [ "$admin_user" = "$USERNAME" ] && [ -n "$NEW_ID" ]; then
            echo "[kc-seed-users] Assigning 'admin' realm role to ${USERNAME} ..."
            ROLE_JSON=$(curl -sf --max-time 10 \
                -H "Authorization: Bearer ${TOKEN}" \
                "${KC_URL}/admin/realms/${REALM}/roles/admin" 2>/dev/null || true)
            curl -sf --max-time 10 \
                -X POST \
                -H "Authorization: Bearer ${TOKEN}" \
                -H "Content-Type: application/json" \
                "${KC_URL}/admin/realms/${REALM}/users/${NEW_ID}/role-mappings/realm" \
                -d "[${ROLE_JSON}]" \
                -o /dev/null
        fi
    done
done

echo "[kc-seed-users] Done."
