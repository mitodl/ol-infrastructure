#!/usr/bin/env bash
# pg-restore.sh — load pg-backup.sh archives into the local-dev Postgres
#
# Imports keycloak-users.json into the olapps realm, then replaces every
# database that has a <db>.dump in DIR: DROP ... WITH (FORCE),
# CREATE DATABASE ... OWNER app, pg_restore --exit-on-error. A failed restore
# drops the incomplete database; re-running the script is the recovery.
#
# Keycloak's partialImport endpoint preserves the id on each imported user,
# and ifResourceExists: OVERWRITE deletes and recreates a same-email user
# under that id. Importing users before restoring the databases means
# users_user.global_id — the Keycloak user id the apps store — still points
# at a real user with no reconciliation step. A user that already exists in
# the new realm under the same email (the kc-seed-users trio, most often) is
# replaced by the backed-up one, keeping its old id and password.
#
# The apps may be running. They connect as `app` and run `migrate` on startup,
# so each database is created with CONNECTION LIMIT 0 (superusers are exempt,
# so pg_restore is not) and the limit is lifted only once its archive has
# loaded; an app that connects mid-restore is refused rather than allowed to
# migrate a half-loaded schema. DROP ... WITH (FORCE) ends the apps' existing
# sessions, and their pods reconnect once the limit lifts. A pod that was
# already serving does not re-run migrate, so if the archive predates the
# checkout's migrations, restart the app Deployments afterwards.
#
# Usage:
#   ./local-dev/scripts/pg-restore.sh DIR [--yes]
#
# --yes skips the confirmation prompt (needed when stdin is not a terminal).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

log()  { echo "▶ $*"; }
ok()   { echo "  ✓ $*"; }
warn() { echo "  ⚠ $*"; }
err()  { echo "  ✗ $*" >&2; exit 1; }

DIR=""
YES=0
while [ $# -gt 0 ]; do
    case "$1" in
        --yes) YES=1; shift ;;
        -*) err "unknown argument: $1 (usage: pg-restore.sh DIR [--yes])" ;;
        *) [ -z "$DIR" ] || err "unexpected argument: $1 (usage: pg-restore.sh DIR [--yes])"; DIR="$1"; shift ;;
    esac
done
[ -n "$DIR" ] && [ -d "$DIR" ] || err "usage: pg-restore.sh DIR [--yes] (DIR must be a pg-backup.sh output directory)"

PG=(kubectl -n local-infra exec local-pg-1 -c postgres --)
PGI=(kubectl -n local-infra exec -i local-pg-1 -c postgres --)
# sql DB "STATEMENT" — quiet, unaligned, tuples-only; every error is fatal.
sql() { "${PG[@]}" psql -U postgres -qAt -v ON_ERROR_STOP=1 -d "$1" -c "$2"; }

kubectl -n local-infra wait --for=condition=Ready pod/local-pg-1 --timeout=60s > /dev/null \
    || err "pod local-pg-1 is not Ready"

# --- Validate every archive before touching the cluster ---------------------
DBS=()
[ -f "$DIR/manifest.txt" ] || warn "no manifest.txt in $DIR; skipping the size check"
for f in "$DIR"/*.dump; do
    [ -e "$f" ] || err "no *.dump files in $DIR"
    db=$(basename "$f" .dump)
    [[ "$db" =~ ^[a-z_][a-z0-9_]*$ ]] || err "refusing unsafe database name in $f"
    case "$db" in
        keycloak | postgres | template*) err "refusing to restore $db (see header comment)" ;;
    esac
    [ "$(head -c 5 "$f")" = "PGDMP" ] || err "$f is not a pg_dump custom-format archive"
    if [ -f "$DIR/manifest.txt" ]; then
        want=$(awk -v d="$db" '$1 == d { print $2 }' "$DIR/manifest.txt")
        have=$(wc -c < "$f" | tr -d ' ')
        [ "$want" = "$have" ] || err "$f is $have bytes but manifest.txt says ${want:-<not listed>}; truncated or partial backup?"
    fi
    DBS+=("$db")
done
if [ -f "$DIR/manifest.txt" ]; then
    listed=$(wc -l < "$DIR/manifest.txt" | tr -d ' ')
    [ "$listed" = "${#DBS[@]}" ] || err "manifest.txt lists $listed databases but $DIR has ${#DBS[@]} dumps; incomplete copy?"
fi

# --- Keycloak preconditions ---------------------------------------------------
command -v curl > /dev/null || err "curl not found"
command -v jq > /dev/null || err "jq not found"

_ROOT_DOMAIN="${LOCAL_DEV_ROOT_DOMAIN:-mit.dev}"
KC_URL="${KC_URL:-https://sso.ol.${_ROOT_DOMAIN}}"
# The ingress serves an mkcert-issued cert whose root CA is not in the system
# trust store, so curl needs it explicitly or the handshake fails (exit 60)
# and get_token's retry loop misreports a healthy Keycloak as still starting.
KC_CACERT="${KC_CACERT:-${REPO_ROOT}/local-dev/certs/rootCA.pem}"
[ -f "$KC_CACERT" ] || err "CA certificate not found at $KC_CACERT (run local-dev/scripts/setup.sh to generate local certs)"
KC_USER="admin"
KC_PASS="admin"  # pragma: allowlist secret

get_token() {
    local token attempt
    for attempt in 1 2 3 4 5; do
        token=$(curl -sf --cacert "$KC_CACERT" --max-time 10 \
            -X POST "$KC_URL/realms/master/protocol/openid-connect/token" \
            -d client_id=admin-cli -d grant_type=password \
            -d "username=$KC_USER" -d "password=$KC_PASS" 2>/dev/null \
            | jq -r '.access_token // empty' 2>/dev/null || true)
        [ -n "$token" ] && [ "$token" != "null" ] && { echo "$token"; return 0; }
        echo "  … attempt $attempt/5: waiting for Keycloak" >&2
        sleep 5
    done
    err "could not obtain an admin token from $KC_URL"
}

KC_USERS_FILE="$DIR/keycloak-users.json"
if [ -f "$KC_USERS_FILE" ]; then
    jq -e 'type == "array" and length > 0' "$KC_USERS_FILE" > /dev/null \
        || err "$KC_USERS_FILE must be a non-empty JSON array"
    KC_N=$(jq 'length' "$KC_USERS_FILE")
    # Fetched before the prompt so an unreachable Keycloak fails the run before
    # you commit to it; the import fetches a fresh one, the token is short-lived.
    get_token > /dev/null
else
    warn "no keycloak-users.json in $DIR; users will not be imported"
    KC_USERS_FILE=""
fi

# --- Plan + confirm ----------------------------------------------------------
log "Plan (archives from $DIR):"
for db in "${DBS[@]}"; do
    cur=$(sql postgres "SELECT pg_size_pretty(pg_database_size(datname)) FROM pg_database WHERE datname = '$db'")
    echo "    $db: replace ${cur:-<absent>} with $(wc -c < "$DIR/$db.dump" | tr -d ' ')-byte archive"
done
if [ -n "$KC_USERS_FILE" ]; then
    echo "    Keycloak: import $KC_N users into realm olapps (existing users with the same email are replaced, keeping their old ids)"
fi
if [ "$YES" -ne 1 ]; then
    [ -t 0 ] || err "stdin is not a terminal; pass --yes to skip the confirmation"
    read -r -p "Type 'restore' to replace these databases: " answer || err "aborted"
    [ "$answer" = "restore" ] || err "aborted"
fi

# --- Import Keycloak users ----------------------------------------------------
if [ -n "$KC_USERS_FILE" ]; then
    log "Importing users into realm olapps"
    TOKEN=$(get_token)
    body=$(jq -c '{ifResourceExists: "OVERWRITE", users: .}' "$KC_USERS_FILE" \
        | curl -sS --fail-with-body --cacert "$KC_CACERT" -H "Authorization: Bearer $TOKEN" \
            -H 'Content-Type: application/json' -X POST "$KC_URL/admin/realms/olapps/partialImport" -d @-) \
        || { echo "$body" >&2; err "Keycloak partialImport failed"; }
    got=$(jq -r '.added + .overwritten + .skipped' <<< "$body")
    [ "$got" = "$KC_N" ] || err "Keycloak applied $got of $KC_N users: $body"
    ok "Keycloak: $(jq -r '"\(.added) added, \(.overwritten) overwritten, \(.skipped) skipped"' <<< "$body")"
fi

# --- Restore -----------------------------------------------------------------
for db in "${DBS[@]}"; do
    log "Restoring $db"
    sql postgres "DROP DATABASE IF EXISTS \"$db\" WITH (FORCE)"
    sql postgres "CREATE DATABASE \"$db\" OWNER app TEMPLATE template0 CONNECTION LIMIT 0"
    if ! "${PGI[@]}" pg_restore -U postgres --no-password -d "$db" --exit-on-error < "$DIR/$db.dump"; then
        sql postgres "DROP DATABASE IF EXISTS \"$db\" WITH (FORCE)" || warn "could not drop the incomplete $db"
        err "pg_restore failed for $db; the incomplete database was dropped. Fix the cause and re-run."
    fi
    sql postgres "ALTER DATABASE \"$db\" CONNECTION LIMIT -1"
    ok "$db restored"
done

ok "Done. The apps reconnect on their own; then: tilt trigger seed-mit-learn-opensearch && tilt trigger seed-mit-learn-qdrant"
