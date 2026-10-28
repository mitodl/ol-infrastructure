#!/usr/bin/env bash
# kc-user-id.sh: print the olapps Keycloak user id for an email address.
#
# Usage:
#   ./local-dev/scripts/kc-user-id.sh admin@ol.test
#
# The apps store this id as users_user.global_id. Exits non-zero when the realm
# has no such user, which for the kc-seed-users trio means that resource has
# not run yet.

set -euo pipefail

email="$1"
# shellcheck source=/dev/null
source "$(dirname "${BASH_SOURCE[0]}")/kc-admin.sh"

TOKEN=$(get_token)
user_id=$(kc_user_id "${TOKEN}" "${email}")
if [ -z "${user_id}" ]; then
    echo "[kc-user-id] ERROR: no ${email} in the ${REALM} realm at ${KC_URL}." >&2
    echo "[kc-user-id] Run the kc-seed-users resource (tilt trigger kc-seed-users) first." >&2
    exit 1
fi
echo "${user_id}"
