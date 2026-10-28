# shellcheck shell=bash
# kc-admin.sh — Keycloak admin API access shared by the kc-*.sh scripts.
#
# Source it from a script; it defines KC_URL, REALM, KC_CACERT and get_token,
# resolving the Keycloak URL from LOCAL_DEV_ROOT_DOMAIN (KC_URL overrides it).
# Messages are tagged with the sourcing script's name.

LOG_TAG="$(basename "${BASH_SOURCE[1]}" .sh)"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

_ROOT_DOMAIN="${LOCAL_DEV_ROOT_DOMAIN:-mit.test}"
KC_URL="${KC_URL:-https://sso.ol.${_ROOT_DOMAIN}}"
# shellcheck disable=SC2034  # read by the scripts that source this file
REALM="olapps"
KC_USER="admin"
KC_PASS="admin"  # pragma: allowlist secret

# The ingress serves an mkcert-issued cert (setup.sh generates it) whose root CA
# is not in the system trust store, so every curl must validate against that CA
# explicitly. Without it curl exits 60 on the handshake, the token comes back
# empty, and get_token's retry loop reports it as "waiting for Keycloak" --
# which sends you looking at a Keycloak that was healthy the whole time.
KC_CACERT="${KC_CACERT:-${REPO_ROOT}/local-dev/certs/rootCA.pem}"
if [ ! -f "${KC_CACERT}" ]; then
    echo "[${LOG_TAG}] ERROR: CA certificate not found at ${KC_CACERT}" >&2
    echo "[${LOG_TAG}] Run local-dev/scripts/setup.sh to generate the local certs." >&2
    exit 1
fi

# Print an admin access token, retrying up to 5 times.
get_token() {
    local token attempt
    for attempt in 1 2 3 4 5; do
        token=$(curl -sf --cacert "${KC_CACERT}" --max-time 10 \
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
        echo "[${LOG_TAG}] attempt ${attempt}/5: waiting for Keycloak..." >&2
        sleep 5
    done
    echo "[${LOG_TAG}] ERROR: could not obtain admin token from ${KC_URL}" >&2
    return 1
}
