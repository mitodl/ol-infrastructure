#!/usr/bin/env bash
# test-db-env.sh — hand out an isolated Postgres test-database name for
# host-run tests, and sweep away stale ones left by previous runs.
#
# Django (mit-learn, mitxonline, ...) builds DATABASES from DATABASE_URL, and
# pytest-django / `manage.py test` create+drop a separate `test_<name>`
# database for the actual run — the `<name>` database itself is never opened.
# Pointing DATABASE_URL at a name unique to your worktree/branch means
# concurrent agents/worktrees never collide on the same `test_<name>`
# database, and the shared `mitlearn`/`mitxonline` databases the
# interactively-running pods use are never touched by a test run.
#
# Usage:
#   eval "$(local-dev/scripts/test-db-env.sh my-branch)"
#   DATABASE_URL=... uv run pytest ...
#
# Only stdout is the eval-able `export DATABASE_URL=...` line; progress/sweep
# logging goes to stderr so it's safe to eval the whole output.
#
# The <slug> becomes part of the throwaway database name, suffixed with the
# current time so stale entries (crashed runs, killed agents) can be swept by
# age. Sweeping happens as a side effect of every invocation, not via a
# separate always-on process: unlike Docker image growth (disk-janitor.sh),
# test databases are only ever created at the moment this script runs, so
# sweeping at that same moment is sufficient.
#
# Knobs:
#   TEST_DB_MAX_AGE_HOURS              drop test_* databases older than this
#                                       (default 2 — our suites run 5-15 min;
#                                       bump it for a deliberate `pytest
#                                       --keepdb` session that outlives that)
#   TEST_DB_HOST / TEST_DB_PORT        default localhost / 15432, matching
#                                       cluster/k3d-config.yaml's port mapping
#   TEST_DB_USER / TEST_DB_PASSWORD    default app / localdev, matching the
#                                       pg-app-credentials Secret in
#                                       infra/modules/database.py
#
# Requires only kubectl (already a local-dev prerequisite) — the sweep runs
# psql inside the local-pg-1 pod rather than requiring a Postgres client on
# the host; only the pytest process itself needs host-to-cluster TCP access.

set -uo pipefail

SLUG="${1:?usage: test-db-env.sh <slug>}"
MAX_AGE_HOURS="${TEST_DB_MAX_AGE_HOURS:-2}"
DB_HOST="${TEST_DB_HOST:-localhost}"
DB_PORT="${TEST_DB_PORT:-15432}"
DB_USER="${TEST_DB_USER:-app}"
DB_PASSWORD="${TEST_DB_PASSWORD:-localdev}"  # pragma: allowlist secret
PG_POD="local-pg-1"
PG_NAMESPACE="local-infra"

ts() { date "+%Y-%m-%d %H:%M:%S"; }
log() { echo "[$(ts)] $*" >&2; }

if [[ ! "$MAX_AGE_HOURS" =~ ^[0-9]+$ ]]; then
    log "invalid TEST_DB_MAX_AGE_HOURS='${MAX_AGE_HOURS}' — using 2"
    MAX_AGE_HOURS=2
fi

# Lowercase alnum/hyphen only, so it's safe to interpolate into SQL below and
# is a valid Postgres identifier fragment.
SAFE_SLUG="$(printf '%s' "$SLUG" | tr '[:upper:]' '[:lower:]' | tr -c 'a-z0-9-' '-')"
NOW_EPOCH="$(date +%s)"
DB_NAME="${SAFE_SLUG}-${NOW_EPOCH}"

psql_exec() {
    # A bare `-U app` would connect over the pod's Unix socket, which uses
    # peer auth and fails for a `kubectl exec`'d process (its OS user doesn't
    # map to the `app` Postgres role) — force TCP loopback + password auth
    # instead, same credentials as pg-app-credentials in database.py.
    kubectl exec -n "$PG_NAMESPACE" "$PG_POD" -- \
        psql "postgresql://app:${DB_PASSWORD}@127.0.0.1:5432/postgres" -tAc "$1" 2>/dev/null
}

# ---------------------------------------------------------------------------
# Sweep test_* databases older than TEST_DB_MAX_AGE_HOURS. See header comment
# for why this runs here instead of as a separate always-on janitor.
# ---------------------------------------------------------------------------
sweep_stale() {
    local cutoff=$(( NOW_EPOCH - MAX_AGE_HOURS * 3600 ))
    local name epoch in_use

    while IFS= read -r name; do
        [[ -z "$name" ]] && continue
        # Expect names shaped test_<slug>-<epoch>; anything else wasn't
        # created by this script and is left alone.
        epoch="${name##*-}"
        [[ "$epoch" =~ ^[0-9]+$ ]] || continue
        (( epoch >= cutoff )) && continue

        in_use="$(psql_exec "SELECT count(*) FROM pg_stat_activity WHERE datname = '${name}' AND pid <> pg_backend_pid();")"
        if [[ "${in_use:-0}" -gt 0 ]]; then
            log "skipping ${name} (still has an active connection)"
            continue
        fi

        # A failed DROP here (e.g. another invocation's sweep already removed
        # it, a benign race) is logged, not fatal — the postcondition, "this
        # name isn't lingering", already holds either way.
        if psql_exec "DROP DATABASE IF EXISTS \"${name}\";" >/dev/null; then
            log "dropped stale test database ${name}"
        else
            log "failed to drop ${name} (continuing)"
        fi
    done < <(psql_exec "SELECT datname FROM pg_database WHERE datname LIKE 'test\\_%';")
}

sweep_stale

log "handing out database name ${DB_NAME} (Django's test runner creates/drops test_${DB_NAME})"
echo "export DATABASE_URL=\"postgresql://${DB_USER}:${DB_PASSWORD}@${DB_HOST}:${DB_PORT}/${DB_NAME}?sslmode=disable\""
