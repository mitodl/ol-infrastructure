#!/usr/bin/env bash
# kc-fix-org-claim.sh — Give the olapps realm's organization claim its deployed shape.
#
# Keycloak 26 creates the "organization" client scope itself when a realm has
# organizations enabled, which is why keycloak.py does not create it (the
# deployed realms predate that and get it from org_flows.create_organization_scope).
# The built-in mapper emits the claim as a list of organization aliases. The
# deployed mapper sets addOrganizationId and addOrganizationAttributes, which
# turns the claim into a map of alias to {id, attributes}, and consumers key on
# that id: ol-analytics-api's org-manager gate matches the Keycloak organization
# UUID against it and refuses every request when the claim is a list.
#
# Pulumi cannot hold the built-in mapper (it has no fixed id to import), so this
# idempotent script sets the deployed config on it after every apps apply.
#
# Usage:
#   ./local-dev/scripts/kc-fix-org-claim.sh

set -euo pipefail

# shellcheck source=local-dev/scripts/kc-admin.sh disable=SC1091  # followed only with -x
source "$(dirname "${BASH_SOURCE[0]}")/kc-admin.sh"

# Same values as the deployed mapper in
# src/ol_infrastructure/substructure/keycloak/org_flows.py.
DESIRED_CONFIG='{"addOrganizationAttributes": "true", "addOrganizationId": "true", "jsonType.label": "JSON"}'

kc() {
    curl -sf --cacert "${KC_CACERT}" --max-time 10 \
        -H "Authorization: Bearer ${TOKEN}" "$@"
}

echo "[kc-fix-org-claim] Connecting to Keycloak at ${KC_URL} ..."
TOKEN=$(get_token)

SCOPE_ID=$(kc "${KC_URL}/admin/realms/${REALM}/client-scopes" \
    | jq -r '.[] | select(.name == "organization") | .id')
if [ -z "${SCOPE_ID}" ]; then
    echo "[kc-fix-org-claim] ERROR: no 'organization' client scope in ${REALM}; is organizations_enabled set on the realm?" >&2
    exit 1
fi

MAPPERS_URL="${KC_URL}/admin/realms/${REALM}/client-scopes/${SCOPE_ID}/protocol-mappers/models"
MAPPER=$(kc "${MAPPERS_URL}" \
    | jq -c '.[] | select(.protocolMapper == "oidc-organization-membership-mapper")')
if [ -z "${MAPPER}" ]; then
    echo "[kc-fix-org-claim] ERROR: the 'organization' scope has no organization membership mapper" >&2
    exit 1
fi

if echo "${MAPPER}" | jq -e --argjson want "${DESIRED_CONFIG}" \
    '.config | to_entries | map(select(.key as $k | $want | has($k))) | from_entries == $want' >/dev/null; then
    echo "[kc-fix-org-claim] Organization claim already carries ids, skipping."
    exit 0
fi

echo "[kc-fix-org-claim] Setting addOrganizationId/addOrganizationAttributes on the organization mapper ..."
kc -X PUT -H "Content-Type: application/json" \
    "${MAPPERS_URL}/$(echo "${MAPPER}" | jq -r '.id')" \
    -d "$(echo "${MAPPER}" | jq --argjson want "${DESIRED_CONFIG}" '.config += $want')" \
    -o /dev/null

echo "[kc-fix-org-claim] Done. Sessions created before this change keep the old claim until the user logs in again."
