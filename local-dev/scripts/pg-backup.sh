#!/usr/bin/env bash
# pg-backup.sh — dump the local-dev app databases before a cluster recreate
#
# Every PersistentVolume in the k3d cluster is local-path storage inside a node
# container, so teardown.sh (and any k3d-config.yaml node-image change, which
# needs a teardown) deletes all Postgres data. This writes one `pg_dump -Fc`
# archive per app database plus a manifest of byte counts into DIR, for
# pg-restore.sh to load into the new cluster.
#
# Dumped: every database except postgres, the templates, and
#   keycloak  — Pulumi recreates the realm; restoring the old database under
#               it is the "404 Realm not found" failure teardown.sh describes.
#               Its users are carried separately, in keycloak-users.json.
#   litellm   — config-file driven; its tables hold only runtime rows
#   app       — CNPG's bootstrap database, always empty
#   test_*    — pytest's leftover test databases
#
# Keycloak assigns each user its id at creation, and the apps store that id in
# users_user.global_id. keycloak-users.json exports the olapps realm's users
# (with ids, hashed passwords, attributes, realm roles) so pg-restore.sh can
# re-import them under the same ids before loading the databases, keeping
# users_user.global_id valid with no reconciliation step.
#
# pg_dump runs inside the local-pg-1 pod through `kubectl exec`, so the host
# needs no Postgres client tools.
#
# Usage:
#   ./local-dev/scripts/pg-backup.sh [--out DIR]
#
# DIR defaults to local-dev/.backups/<UTC timestamp>/ (gitignored).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

log()  { echo "▶ $*"; }
ok()   { echo "  ✓ $*"; }
warn() { echo "  ⚠ $*"; }
err()  { echo "  ✗ $*" >&2; exit 1; }

OUT="${REPO_ROOT}/local-dev/.backups/$(date -u +%Y%m%dT%H%M%SZ)"
while [ $# -gt 0 ]; do
    case "$1" in
        --out) OUT="${2:?--out needs a directory}"; shift 2 ;;
        *) err "unknown argument: $1 (usage: pg-backup.sh [--out DIR])" ;;
    esac
done

PG=(kubectl -n local-infra exec local-pg-1 -c postgres --)

if [ -e "$OUT" ] && [ -n "$(ls -A "$OUT")" ]; then
    err "refusing to write into non-empty $OUT"
fi
mkdir -p "$OUT"

log "Listing databases"
dbs=$("${PG[@]}" psql -U postgres -At -c \
    "SELECT datname FROM pg_database
      WHERE NOT datistemplate
        AND datname NOT IN ('postgres', 'keycloak', 'litellm', 'app')
        AND datname NOT LIKE 'test\_%'
      ORDER BY 1")
[ -n "$dbs" ] || err "no databases to dump"

for db in $dbs; do
    # pg-restore.sh refuses any other name; better to hear that before the teardown.
    [[ "$db" =~ ^[a-z_][a-z0-9_]*$ ]] || err "$db cannot be restored by pg-restore.sh; drop or rename it, then re-run"
    log "Dumping $db"
    "${PG[@]}" pg_dump -U postgres --no-password -Fc "$db" > "$OUT/$db.dump.part"
    [ "$(head -c 5 "$OUT/$db.dump.part")" = "PGDMP" ] || err "$db.dump.part is not a pg_dump custom-format archive"
    mv "$OUT/$db.dump.part" "$OUT/$db.dump"
    bytes=$(wc -c < "$OUT/$db.dump" | tr -d ' ')
    echo "$db $bytes" >> "$OUT/manifest.txt"
    ok "$db: $bytes bytes"
done

log "Exporting olapps realm users"
# Service-account users are excluded because Pulumi recreates the clients that
# own them. Realm roles are exported by name, default-roles-olapps included:
# partialImport grants only the roles listed, so leaving it out would strip
# the imported users of it.
kubectl -n local-infra exec -i local-pg-1 -c postgres -- \
    psql -U postgres -qAt -v ON_ERROR_STOP=1 -d keycloak -f - > "$OUT/keycloak-users.json" <<'SQL'
SELECT coalesce(json_agg(json_build_object(
  'id', u.id,
  'username', u.username,
  'email', u.email,
  'emailVerified', u.email_verified,
  'enabled', u.enabled,
  'firstName', u.first_name,
  'lastName', u.last_name,
  'createdTimestamp', u.created_timestamp,
  'attributes', (SELECT coalesce(json_object_agg(a.name, a.vals), '{}'::json)
                   FROM (SELECT name, json_agg(coalesce(long_value, value)) AS vals
                           FROM user_attribute WHERE user_id = u.id GROUP BY name) a),
  'credentials', (SELECT coalesce(json_agg(json_build_object(
                    'type', c.type, 'userLabel', c.user_label, 'priority', c.priority,
                    'createdDate', c.created_date, 'secretData', c.secret_data,
                    'credentialData', c.credential_data)), '[]'::json)
                    FROM credential c WHERE c.user_id = u.id),
  'realmRoles', (SELECT coalesce(json_agg(k.name), '[]'::json)
                   FROM user_role_mapping m JOIN keycloak_role k ON k.id = m.role_id
                  WHERE m.user_id = u.id AND NOT k.client_role)
) ORDER BY u.email), '[]'::json)
FROM user_entity u JOIN realm r ON r.id = u.realm_id
WHERE r.name = 'olapps' AND u.service_account_client_link IS NULL;
SQL
jq -e 'type == "array"' "$OUT/keycloak-users.json" > /dev/null || err "keycloak-users.json is not a JSON array"
n=$(jq 'length' "$OUT/keycloak-users.json")
if [ "$n" -eq 0 ]; then
    warn "keycloak-users.json: 0 users"
else
    ok "keycloak-users.json: $n users"
fi

ok "Wrote $OUT"
echo "  Restore with: ./local-dev/scripts/pg-restore.sh $OUT"
