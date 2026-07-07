#!/usr/bin/env bash
# seed.sh — Run seeding / data-loading commands inside a running app pod.
#
# Usage:
#   ./local-dev/scripts/seed.sh --app <name>              # run all seed commands for an app
#   ./local-dev/scripts/seed.sh --app <name> --cmd <cmd>  # run a single management command
#   ./local-dev/scripts/seed.sh --list                    # list available apps and their defaults
#
# Examples:
#   ./local-dev/scripts/seed.sh --app mit-learn
#   ./local-dev/scripts/seed.sh --app mit-learn --cmd "loaddata platforms schools departments offered_by"
#   ./local-dev/scripts/seed.sh --app mitxonline --cmd "configure_instance"
#   ./local-dev/scripts/seed.sh --app mit-learn --cmd "backpopulate_ocw_data"
#
# The script delegates execution to `kubectl exec` — the target namespace and
# deployment name are derived from the app name.  The cluster must be running
# and the deployment must be healthy before running this script.

set -euo pipefail

# ---------------------------------------------------------------------------
# App metadata — must stay in sync with the APPS list in the root Tiltfile.
# ---------------------------------------------------------------------------
declare -A APP_NAMESPACE=(
    [mit-learn]="mit-learn"
    [learn-ai]="learn-ai"
    [mitxonline]="mitxonline"
    [odl-video-service]="odl-video-service"
)

declare -A APP_DEPLOY=(
    [mit-learn]="mitlearn-webapp"
    [learn-ai]="learnai-webapp"
    [mitxonline]="mitxonline-webapp"
    [odl-video-service]="odlvideo-webapp"
)

# Default seed commands run when --app is given without --cmd.
# Each entry is a plain shell command executed inside the pod under /src.
declare -A APP_DEFAULT_SEED=(
    [mit-learn]="python manage.py migrate --noinput
RUN_DATA_MIGRATIONS=true python manage.py migrate --noinput
python manage.py createcachetable
python manage.py loaddata platforms schools departments offered_by
python manage.py create_qdrant_collections
python manage.py prune_subscription_queries"

    [learn-ai]="python manage.py migrate --noinput
RUN_DATA_MIGRATIONS=true python manage.py migrate --noinput
python manage.py createcachetable"

    [mitxonline]="python manage.py migrate --noinput
python manage.py configure_wagtail
python manage.py configure_instance"

    [odl-video-service]="python manage.py migrate --noinput
python manage.py createpresets"
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
log()  { echo "[seed] $*"; }
err()  { echo "[seed] ERROR: $*" >&2; exit 1; }

exec_in_pod() {
    local namespace="$1"
    local deploy="$2"
    local cmd="$3"

    log "kubectl exec -n ${namespace} deploy/${deploy} -- bash -c '${cmd}'"
    kubectl exec -n "${namespace}" "deploy/${deploy}" -- bash -c "${cmd}"
}

list_apps() {
    echo "Available apps:"
    for app in "${!APP_NAMESPACE[@]}"; do
        echo "  ${app}  (namespace: ${APP_NAMESPACE[$app]}, deploy: ${APP_DEPLOY[$app]})"
    done
    echo ""
    echo "Default seed sequences:"
    for app in "${!APP_DEFAULT_SEED[@]}"; do
        echo ""
        echo "  ${app}:"
        while IFS= read -r line; do
            echo "    ${line}"
        done <<< "${APP_DEFAULT_SEED[$app]}"
    done
}

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------
APP=""
CUSTOM_CMD=""
DO_LIST=false

while [[ $# -gt 0 ]]; do
    case "$1" in
        --app)
            APP="$2"
            shift 2
            ;;
        --cmd)
            CUSTOM_CMD="$2"
            shift 2
            ;;
        --list)
            DO_LIST=true
            shift
            ;;
        -h|--help)
            echo "Usage: seed.sh [--app <name>] [--cmd <management-command>] [--list]"
            exit 0
            ;;
        *)
            err "Unknown argument: $1"
            ;;
    esac
done

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
if $DO_LIST; then
    list_apps
    exit 0
fi

[[ -z "$APP" ]] && err "--app is required. Use --list to see available apps."

# Validate app name
[[ -z "${APP_NAMESPACE[$APP]+_}" ]] && err "Unknown app '${APP}'. Use --list to see available apps."

NS="${APP_NAMESPACE[$APP]}"
DEPLOY="${APP_DEPLOY[$APP]}"

# Verify the deployment is available
if ! kubectl get deploy "${DEPLOY}" -n "${NS}" &>/dev/null; then
    err "Deployment '${DEPLOY}' not found in namespace '${NS}'. Is the stack running?"
fi

if [[ -n "$CUSTOM_CMD" ]]; then
    # Run a single user-specified management command
    log "Running custom command in ${APP}..."
    exec_in_pod "${NS}" "${DEPLOY}" "${CUSTOM_CMD}"
else
    # Run the full default seed sequence for this app, one command at a time
    log "Running default seed sequence for ${APP}..."
    while IFS= read -r cmd; do
        [[ -z "$cmd" ]] && continue
        exec_in_pod "${NS}" "${DEPLOY}" "${cmd}"
    done <<< "${APP_DEFAULT_SEED[$APP]}"
fi

log "Done."
