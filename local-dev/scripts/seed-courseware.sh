#!/usr/bin/env bash
#
# Populate test courseware into mitxonline *and* the local Open edX.
#
# Run from the Tilt UI (`seed-courseware`, manual trigger) or by hand:
#
#     ./local-dev/scripts/seed-courseware.sh
#     ./local-dev/scripts/seed-courseware.sh --openedx none   # mitxonline only
#
# Everything it creates comes from local-dev/data/courseware-seed.json, which is
# the file to edit to add courses. Both sides read the same file, which is the
# point: a course run only works end to end if mitxonline's courseware_id and
# the Open edX course key agree exactly.
#
# What lands where:
#   Open edX (Studio)  empty course shells, one run per course
#   Open edX (LMS)     audit + verified enrollment modes per run
#   mitxonline         Programs, Courses, CourseRuns, published Wagtail pages,
#                      Products, a 100%-off discount, financial-assistance tiers
#
# Idempotent throughout, so it is safe to re-run after editing the seed file.
#
# Deliberately NOT auto-run by Tilt: it is slow, and a developer who has been
# editing courseware by hand should choose when it reasserts itself.
#
# Prerequisites: the mitxonline deployment is up, and (unless --openedx none)
# tutor is running with `openedx-tutor-seed` already applied -- the Studio calls
# below authenticate as the service worker that resource creates.
set -euo pipefail

ROOT_DOMAIN="${LOCAL_DEV_ROOT_DOMAIN:-mit.dev}"
TUTOR="${TUTOR_BIN:-tutor}"

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &>/dev/null && pwd)
PAYLOAD_DIR="${SCRIPT_DIR}/seed-courseware"
DATA_FILE="${SCRIPT_DIR}/../data/courseware-seed.json"

# Must match tutor-seed-mitxonline.sh: the courses below are created *as* this
# user, and Studio requires an account with course-creation rights.
#
# The *email*, not the username, is what identifies it to generate_courses:
# that command resolves its `user` field through contentstore's user_from_str,
# which accepts an email address or a numeric id and nothing else. Passing the
# username gets a per-course "user does not exist" warning and a zero exit.
SERVICE_WORKER_USERNAME="mitxonline_service_worker"
SERVICE_WORKER_EMAIL="mitxonline-service-worker@${ROOT_DOMAIN}"

MITXONLINE_NS="mitxonline"
MITXONLINE_DEPLOY="mitxonline-webapp"

MITLEARN_NS="mit-learn"
MITLEARN_DEPLOY="mitlearn-webapp"
# backpopulate_mitxonline_data hands the ETL to Celery and blocks on the
# result, so the worker matters as much as the web pod. local-dev runs with
# CELERY_TASK_ALWAYS_EAGER=False, and get_mitxonline_data is an unrouted task,
# so it lands on the default queue.
MITLEARN_WORKER_DEPLOY="mitlearn-worker-default"
# The ETL walks every mitxonline course, not just the seeded ones, so give it
# considerably longer than any other step here.
MITLEARN_INGEST_TIMEOUT="600"

OPENEDX_MODE="tutor"
# auto: ingest into MIT Learn if the deployment is there, skip with a warning
# if it is not (a `--enabled_apps mitxonline` stack has no mit-learn).
LEARN_MODE="auto"

log()  { echo "▶ $*"; }
ok()   { echo "  ✓ $*"; }
warn() { echo "  ⚠ $*"; }
err()  { echo "  ✗ $*" >&2; exit 1; }

usage() {
    cat <<'USAGE'
Usage: seed-courseware.sh [--openedx tutor|none] [--learn auto|yes|no] [--data <path>]

  --openedx tutor  create the courses in the local tutor instance (default)
  --openedx none   mitxonline only; use in openedx_mode "qa", where there is
                   no local Open edX to create them in
  --learn auto     ingest into MIT Learn when mit-learn is deployed (default)
  --learn yes      always ingest; fail if mit-learn is not deployed
  --learn no       skip the MIT Learn ingestion entirely
  --data <path>    seed file to read (default: local-dev/data/courseware-seed.json)
USAGE
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --openedx)
            OPENEDX_MODE="$2"
            shift 2
            ;;
        --learn)
            LEARN_MODE="$2"
            shift 2
            ;;
        --data)
            DATA_FILE="$2"
            shift 2
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            usage >&2
            err "unknown argument: $1"
            ;;
    esac
done

case "${OPENEDX_MODE}" in
    tutor|none) ;;
    *) err "--openedx must be 'tutor' or 'none', got '${OPENEDX_MODE}'" ;;
esac

case "${LEARN_MODE}" in
    auto|yes|no) ;;
    *) err "--learn must be 'auto', 'yes' or 'no', got '${LEARN_MODE}'" ;;
esac

[[ -f "${DATA_FILE}" ]] || err "seed file not found: ${DATA_FILE}"

# The payloads receive the seed file as a Python r"""...""" literal, so these
# two characters would end it early and produce a syntax error a long way from
# its cause. Neither has any business in a courseware id or title.
if grep -qE '\\|"""' "${DATA_FILE}"; then
    err "${DATA_FILE} contains a backslash or a triple quote; neither can be embedded"
fi

# sys.exit(str) rather than letting it raise: the position of the syntax error
# is the useful part, and a traceback buries it.
python3 -c '
import json
import sys

try:
    with open(sys.argv[1]) as handle:
        json.load(handle)
except ValueError as exc:
    sys.exit(f"    {exc}")
' "${DATA_FILE}" || err "${DATA_FILE} is not valid JSON"

# Checked before anything is written, so a failure here cannot leave one side of
# a two-sided change applied. Runs tutor rather than just looking for it -- see
# the note in tutor-configure.sh.
if [[ "${OPENEDX_MODE}" == "tutor" ]]; then
    "${TUTOR}" --version &>/dev/null || {
        echo "  ✗ '${TUTOR}' is not a working tutor. Set TUTOR_BIN to the real one;" >&2
        echo "    ./local-dev/scripts/tutor-configure.sh explains the options." >&2
        exit 1
    }
fi

kubectl get deploy "${MITXONLINE_DEPLOY}" -n "${MITXONLINE_NS}" &>/dev/null \
    || err "deployment ${MITXONLINE_DEPLOY} not found in ${MITXONLINE_NS}. Is the stack up?"

# Resolved here, with the other preflight checks, so "mit-learn is not part of
# this stack" is reported before anything is written rather than as a surprise
# at the end of a long seed.
if [[ "${LEARN_MODE}" != "no" ]]; then
    if kubectl get deploy "${MITLEARN_DEPLOY}" -n "${MITLEARN_NS}" &>/dev/null; then
        LEARN_MODE="yes"
    elif [[ "${LEARN_MODE}" == "yes" ]]; then
        err "deployment ${MITLEARN_DEPLOY} not found in ${MITLEARN_NS}. Enable mit-learn, or pass --learn no."
    else
        warn "mit-learn is not deployed; skipping the MIT Learn ingestion."
        warn "The courses will exist in mitxonline but not in Learn's search or catalog."
        LEARN_MODE="no"
    fi
fi

TMP_DIR=$(mktemp -d)
trap 'rm -rf "${TMP_DIR}"' EXIT

# ---------------------------------------------------------------------------
# Exec helpers
#
# The tutor ones are the same shape as tutor-seed-mitxonline.sh's: -T because
# Tilt has no TTY and `docker compose exec` otherwise fails outright, plus a
# retry when the container went away mid-command (a `tutor dev restart`, a
# settings re-render, Tilt reconciling openedx-tutor) rather than on its own
# merits. Unlike that script's, these read their payload from a file, so a retry
# can feed stdin again.
# ---------------------------------------------------------------------------

# Waits for `tutor dev exec` against $1 to work. The container can be up one
# moment and restarting the next, and exec fails outright while it is.
wait_for_tutor_service() {
    local service="$1"
    local waited=0
    until "${TUTOR}" dev exec -T "${service}" true >/dev/null 2>&1; do
        if (( waited >= 180 )); then
            echo "  ✗ the ${service} container has not accepted an exec in 3 minutes." >&2
            echo "    Check the openedx-tutor resource, then re-run this script." >&2
            exit 1
        fi
        [[ ${waited} -eq 0 ]] && echo "  … waiting for the ${service} container"
        sleep 3
        waited=$((waited + 3))
    done
}

# tutor_exec <service> <stdin-file-or-empty> <command...>
tutor_exec() {
    local service="$1"
    local stdin_file="$2"
    shift 2

    local attempt=1
    while true; do
        if [[ -n "${stdin_file}" ]]; then
            "${TUTOR}" dev exec -T "${service}" "$@" < "${stdin_file}" && return 0
        else
            "${TUTOR}" dev exec -T "${service}" "$@" && return 0
        fi
        # exec still works, so the command failed on its own merits.
        if "${TUTOR}" dev exec -T "${service}" true >/dev/null 2>&1; then
            return 1
        fi
        if (( attempt >= 3 )); then
            echo "  ✗ the ${service} container kept going away; giving up." >&2
            return 1
        fi
        echo "  … the ${service} container went away mid-command, retrying"
        wait_for_tutor_service "${service}"
        attempt=$((attempt + 1))
    done
}

wait_for_mitxonline() {
    kubectl rollout status -n "${MITXONLINE_NS}" "deploy/${MITXONLINE_DEPLOY}" \
        --timeout=180s
}

# mitxonline_exec <stdin-file-or-empty> <command...>
# -c app: the web pod also runs an nginx sidecar, and kubectl would otherwise
# pick whichever container is first.
mitxonline_exec() {
    local stdin_file="$1"
    shift

    local -a kexec=(
        kubectl exec -n "${MITXONLINE_NS}" "deploy/${MITXONLINE_DEPLOY}" -c app
    )
    if [[ -n "${stdin_file}" ]]; then
        kexec+=(-i)
    fi
    kexec+=(--)

    # </dev/null on the no-payload path: these run inside a `while read` loop
    # over a here-string, and an exec that inherited that stdin would eat the
    # remaining entries.
    if [[ -n "${stdin_file}" ]]; then
        "${kexec[@]}" "$@" < "${stdin_file}" && return 0
        wait_for_mitxonline
        "${kexec[@]}" "$@" < "${stdin_file}"
    else
        "${kexec[@]}" "$@" </dev/null && return 0
        wait_for_mitxonline
        "${kexec[@]}" "$@" </dev/null
    fi
}

wait_for_mitlearn() {
    kubectl rollout status -n "${MITLEARN_NS}" "deploy/${MITLEARN_DEPLOY}" \
        --timeout=180s
}

# mitlearn_exec <stdin-file-or-empty> <command...>
# Same shape as mitxonline_exec, including the -c app: the mit-learn web pod
# also runs an nginx sidecar (apps/mit-learn/deployment.yaml).
mitlearn_exec() {
    local stdin_file="$1"
    shift

    local -a kexec=(
        kubectl exec -n "${MITLEARN_NS}" "deploy/${MITLEARN_DEPLOY}" -c app
    )
    if [[ -n "${stdin_file}" ]]; then
        kexec+=(-i)
    fi
    kexec+=(--)

    if [[ -n "${stdin_file}" ]]; then
        "${kexec[@]}" "$@" < "${stdin_file}" && return 0
        wait_for_mitlearn
        "${kexec[@]}" "$@" < "${stdin_file}"
    else
        "${kexec[@]}" "$@" </dev/null && return 0
        wait_for_mitlearn
        "${kexec[@]}" "$@" </dev/null
    fi
}

# Composes a `manage.py shell` payload: the seed file as a Python string
# literal, then the payload itself. Django's shell execs piped stdin, which
# sidesteps the argv size and quoting limits of `shell -c`.
build_payload() {
    local payload="$1"
    local out="$2"

    {
        printf 'SEED_JSON = r"""'
        cat "${DATA_FILE}"
        printf '"""\n'
        cat "${payload}"
    } > "${out}"
}

# The first course in the seed file, used for the end-of-run pointers and the
# post-seed verifications. Prints nothing and returns non-zero if the file
# cannot be read, so every caller can treat it as best-effort.
first_course_id() {
    python3 -c '
import json
import sys

with open(sys.argv[1]) as handle:
    print(json.load(handle)["courses"][0]["readable_id"])
' "${DATA_FILE}" 2>/dev/null
}

# ---------------------------------------------------------------------------
# Phase 1 -- Open edX: course shells
#
# `cms generate_courses` takes its whole course list as one JSON argument and
# skips (with a warning) any course that already exists, which is exactly the
# re-runnable behaviour wanted here. The JSON is derived from the seed file so
# the org/number/run triple cannot drift from mitxonline's courseware_id.
# ---------------------------------------------------------------------------
seed_openedx_courses() {
    log "Creating Open edX course shells"
    wait_for_tutor_service cms

    # generate_courses only *warns* when the instructor is missing, and still
    # exits 0 -- so without this check the whole phase reports success while
    # creating nothing.
    #
    # Looks for a printed sentinel rather than exiting non-zero from the shell:
    # `manage.py shell -c` swallows the payload's exit status, so a sys.exit(1)
    # in there is invisible out here. Captured rather than piped into grep -q,
    # which closes the pipe on its first match and, under pipefail, turns the
    # success case into a failure.
    local probe
    probe=$(tutor_exec cms "" ./manage.py cms shell -c "
from django.contrib.auth import get_user_model

if get_user_model().objects.filter(email='${SERVICE_WORKER_EMAIL}').exists():
    print('OL_SEED_INSTRUCTOR_PRESENT')
" 2>/dev/null || true)

    if [[ "${probe}" != *OL_SEED_INSTRUCTOR_PRESENT* ]]; then
        err "no Open edX account with the email ${SERVICE_WORKER_EMAIL}.
    The ${SERVICE_WORKER_USERNAME} account is created by the openedx-tutor-seed
    Tilt resource (./local-dev/scripts/tutor-seed-mitxonline.sh) -- run that
    first. If it has run, check that LOCAL_DEV_ROOT_DOMAIN matches the one it
    used, since the email is derived from it."
    fi

    local courses_json
    courses_json=$(python3 - "${DATA_FILE}" "${SERVICE_WORKER_EMAIL}" <<'PY'
import json
import sys
from datetime import UTC, datetime, timedelta

data_file, instructor = sys.argv[1], sys.argv[2]

with open(data_file) as handle:
    seed = json.load(handle)

now = datetime.now(tz=UTC)


def stamp(delta_days):
    return (now + timedelta(days=delta_days)).strftime("%Y-%m-%dT%H:%M:%SZ")


courses = []
for course in seed["courses"]:
    # "course-v1:ORG+NUMBER" -> ORG, NUMBER
    org, number = course["readable_id"].split(":", 1)[1].split("+", 1)
    courses.append(
        {
            "organization": org,
            "number": number,
            "run": course["run_tag"],
            "user": instructor,
            "fields": {
                "display_name": course["title"],
                # Same offsets as the mitxonline payload's RUN_DATES, so
                # `sync_courserun` finds the two sides already in agreement.
                "start": stamp(-7),
                "end": stamp(180),
                "enrollment_start": stamp(-14),
                "enrollment_end": stamp(180),
                "self_paced": True,
            },
        }
    )

print(json.dumps({"courses": courses}))
PY
    ) || err "could not build the generate_courses payload from ${DATA_FILE}"

    # generate_courses logs through the logging framework rather than the
    # command's stdout, so a quiet run here is normal. It also exits 0 whatever
    # happens, which is why phase 2 -- not this exit status -- is what decides
    # whether the courses actually exist.
    tutor_exec cms "" ./manage.py cms generate_courses "${courses_json}" \
        || err "generate_courses failed"
    ok "course shells created (existing ones left alone)"
}

# ---------------------------------------------------------------------------
# Phase 2 -- Open edX: enrollment modes
# ---------------------------------------------------------------------------
seed_openedx_modes() {
    log "Setting Open edX enrollment modes"
    wait_for_tutor_service lms

    local payload="${TMP_DIR}/openedx-lms.py"
    build_payload "${PAYLOAD_DIR}/openedx-lms.py" "${payload}"

    tutor_exec lms "${payload}" ./manage.py lms shell \
        || err "could not set the Open edX enrollment modes"
}

# ---------------------------------------------------------------------------
# Phase 3 -- mitxonline: courseware, CMS pages, products, discount, finaid forms
# ---------------------------------------------------------------------------
seed_mitxonline() {
    log "Seeding mitxonline courseware"

    local payload="${TMP_DIR}/mitxonline.py"
    build_payload "${PAYLOAD_DIR}/mitxonline.py" "${payload}"

    mitxonline_exec "${payload}" python manage.py shell \
        || err "could not seed the mitxonline courseware"
}

# ---------------------------------------------------------------------------
# Phase 4 -- mitxonline: financial-assistance tiers
#
# configure_tiers is already idempotent -- it matches the existing
# `<id>-fa-tier<n>-<year>` discounts rather than duplicating them -- so it is
# called straight rather than reimplemented in the payload. Its sibling
# create_finaid_form is not, and is handled in phase 3 instead; see
# `ensure_finaid_form` in seed-courseware/mitxonline.py.
#
# The tiers price a request; the country income thresholds it is measured
# against are a separate seed (`tilt trigger seed-mitxonline-income-thresholds`).
# ---------------------------------------------------------------------------
seed_financial_assistance() {
    log "Configuring financial-assistance tiers"

    local ids
    ids=$(python3 - "${DATA_FILE}" <<'PY'
import json
import sys

with open(sys.argv[1]) as handle:
    seed = json.load(handle)

for kind, key in (("program", "programs"), ("course", "courses")):
    for entry in seed.get(key, []):
        if entry.get("finaid"):
            print(kind, entry["readable_id"])
PY
    ) || err "could not read the financial-assistance entries from ${DATA_FILE}"

    if [[ -z "${ids}" ]]; then
        warn "no entries flagged \"finaid\": true; nothing to configure"
        return 0
    fi

    while read -r kind readable_id; do
        [[ -z "${kind}" ]] && continue
        mitxonline_exec "" python manage.py configure_tiers "--${kind}" "${readable_id}" \
            || err "configure_tiers failed for ${readable_id}"
        ok "financial-assistance tiers for ${readable_id}"
    done <<< "${ids}"
}

# ---------------------------------------------------------------------------
# Phase 5 -- prove the two sides agree
#
# sync_courserun pulls a run's metadata from Open edX using the service-worker
# token, so it exercises the whole chain: mitxonline's credentials, the APISIX
# route to tutor, and the run existing on both sides. A warning rather than a
# failure -- the seed itself has already succeeded by this point.
# ---------------------------------------------------------------------------
verify_sync() {
    local run_id
    run_id=$(python3 - "${DATA_FILE}" <<'PY'
import json
import sys

with open(sys.argv[1]) as handle:
    seed = json.load(handle)

course = seed["courses"][0]
print(f"{course['readable_id']}+{course['run_tag']}")
PY
    ) || return 0

    [[ -n "${run_id}" ]] || return 0

    log "Verifying mitxonline can read ${run_id} from Open edX"
    if mitxonline_exec "" python manage.py sync_courserun --run "${run_id}"; then
        ok "sync_courserun succeeded"
    else
        warn "sync_courserun failed. The courseware is seeded, but mitxonline"
        warn "could not read the run back from Open edX -- check the"
        warn "openedx-tutor-seed resource (service-worker token and OAuth apps)."
    fi
}

# ---------------------------------------------------------------------------
# Phase 6 -- MIT Learn: ingest what was just created
#
# mit-learn does not read mitxonline's database; it pulls the catalog over HTTP
# from MITX_ONLINE_COURSES_API_URL / MITX_ONLINE_PROGRAMS_API_URL (both pointed
# at https://mitxonline.<root_domain> in apps/mit-learn/configmaps/app-env.yaml).
# So this has to run *after* the mitxonline phase, and re-running it is how a
# seed edit reaches Learn.
#
# Worth knowing when this fails: https://learn.<root_domain>/courses/<readable_id>
# does NOT depend on any of this. That page is rendered from mitxonline's own
# Wagtail pages and v2 courses APIs, proxied through api.learn.<root_domain>.
# What ingestion buys is presence in Learn's search, catalog, channels and
# resource drawer.
# ---------------------------------------------------------------------------
ingest_into_learn() {
    log "Ingesting the seeded courseware into MIT Learn"

    # The webapp's bootstrap init container already loads these, so this is
    # normally a no-op -- but it is cheap, and without the mitxonline row in
    # LearningResourcePlatform the ETL's loader drops every course on the floor
    # without raising.
    if mitlearn_exec "" python manage.py loaddata \
        platforms schools departments offered_by >/dev/null; then
        ok "platform/department fixtures present"
    else
        warn "could not load the mit-learn fixtures; ingestion will probably find nothing"
    fi

    # backpopulate_mitxonline_data blocks on the Celery result, so with no
    # worker it hangs rather than fails. Prove the worker is up first, and cap
    # the command anyway -- an unbounded hang here would wedge the Tilt resource.
    if ! kubectl rollout status -n "${MITLEARN_NS}" \
        "deploy/${MITLEARN_WORKER_DEPLOY}" --timeout=180s; then
        warn "${MITLEARN_WORKER_DEPLOY} is not ready; skipping the MIT Learn ingestion."
        warn "Start it, then re-run this script or trigger seed-mit-learn-mitxonline."
        return 0
    fi

    # Not mitlearn_exec: that retries, and a retry of a run that timed out would
    # just spend the timeout again on a task already in flight.
    if timeout "${MITLEARN_INGEST_TIMEOUT}" \
        kubectl exec -n "${MITLEARN_NS}" "deploy/${MITLEARN_DEPLOY}" -c app -- \
        python manage.py backpopulate_mitxonline_data </dev/null; then
        ok "backpopulate_mitxonline_data finished"
    else
        # The pod-side command keeps running after a timeout kills our kubectl,
        # so this is a warning: the ETL may still land shortly after we return.
        warn "backpopulate_mitxonline_data did not finish cleanly"
        warn "(the task runs in ${MITLEARN_WORKER_DEPLOY}, not the web pod -- check its logs)"
        return 0
    fi

    ensure_learn_search_index
    verify_learn_ingestion
}

# Creates the OpenSearch indexes if this stack has never had them.
#
# Indexing itself is not a step here: mit-learn indexes on upsert through its
# SearchIndexPlugin hook, so the ETL above already pushed the seeded courses.
# But it pushes them into an index that has to exist first, and on a fresh
# stack nothing has created one -- seed-mit-learn-opensearch is manual and
# rarely remembered. Without it Learn's search endpoint 404s while the resource
# API happily returns the course, which is a confusing place to land.
#
# Only when absent: `recreate_index --all` rebuilds from scratch, and a
# developer with a populated index should choose when that happens. It is also
# fire-and-forget (the reindex runs as a Celery job), so this returns long
# before search is actually warm.
ensure_learn_search_index() {
    cat > "${TMP_DIR}/check-index.py" <<'CHECK_PY'
import sys

from learning_resources_search.connection import get_conn, get_default_alias_name

sys.exit(0 if get_conn().indices.exists(get_default_alias_name("course")) else 1)
CHECK_PY

    if mitlearn_exec "${TMP_DIR}/check-index.py" python manage.py shell >/dev/null 2>&1; then
        ok "MIT Learn search index already exists"
        return 0
    fi

    log "No MIT Learn search index yet; creating one"
    if mitlearn_exec "" python manage.py recreate_index --all; then
        ok "reindex job started (it finishes in ${MITLEARN_WORKER_DEPLOY}, not here)"
    else
        warn "could not start the reindex. The courses are ingested and reachable"
        warn "through the API, but Learn's search will stay empty until"
        warn "seed-mit-learn-opensearch has run."
    fi
}

# Confirms the first seeded course reached MIT Learn as a *published* resource.
# Unpublished is the interesting failure: the ETL writes the row either way, and
# only published resources are visible through the API, search or catalog.
verify_learn_ingestion() {
    local course_id
    course_id=$(first_course_id) || return 0
    [[ -n "${course_id}" ]] || return 0

    cat > "${TMP_DIR}/verify-learn.py" <<VERIFY_PY
import sys

from learning_resources.models import LearningResource

readable_id = "${course_id}"
resource = LearningResource.objects.filter(readable_id=readable_id).first()

if resource is None:
    sys.exit(f"    {readable_id} did not reach MIT Learn at all")
if not resource.published:
    sys.exit(
        f"    {readable_id} was ingested but is unpublished -- mit-learn wants the"
        " course page live, include_in_learn_catalog set, and an enrollable run"
    )
print(f"  ✓ {readable_id} published in MIT Learn: {resource.url}")
VERIFY_PY

    if mitlearn_exec "${TMP_DIR}/verify-learn.py" python manage.py shell; then
        return 0
    fi
    warn "the seeded course is not a published MIT Learn resource yet."
    warn "It will still render at https://learn.${ROOT_DOMAIN}/courses/${course_id}"
    warn "(that page reads mitxonline directly), but it will not appear in search."
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
log "Seeding test courseware from ${DATA_FILE}"

if [[ "${OPENEDX_MODE}" == "tutor" ]]; then
    seed_openedx_courses
    seed_openedx_modes
else
    warn "--openedx none: skipping Open edX. mitxonline course runs will have"
    warn "no courseware behind them, so enrollment and sync_courserun will fail."
fi

seed_mitxonline
seed_financial_assistance

if [[ "${OPENEDX_MODE}" == "tutor" ]]; then
    verify_sync
fi

if [[ "${LEARN_MODE}" == "yes" ]]; then
    ingest_into_learn
fi

log "Done. Catalog: https://mitxonline.${ROOT_DOMAIN}/catalog/"
# The detail pages are addressed by readable_id, not by the page slug — the
# index pages route on it — so print one rather than leaving it to be guessed.
FIRST_COURSE=$(first_course_id || true)
if [[ -n "${FIRST_COURSE}" ]]; then
    log "A course page: https://mitxonline.${ROOT_DOMAIN}/courses/${FIRST_COURSE}/"
    # No trailing slash: Learn's route is /courses/[readable_id], and the
    # readable_id itself is the last segment.
    log "The same course on Learn: https://learn.${ROOT_DOMAIN}/courses/${FIRST_COURSE}"
fi
if [[ "${LEARN_MODE}" == "yes" ]]; then
    # Filtered by platform rather than by a query string: it stays right however
    # the seed file is edited, and mitxonline is the only platform this stack
    # ingests from.
    log "In Learn search: https://learn.${ROOT_DOMAIN}/search?platform=mitxonline"
fi
log "Checkout without a payment gateway: apply the LOCALDEV100 discount code."
