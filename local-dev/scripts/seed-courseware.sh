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

OPENEDX_MODE="tutor"

log()  { echo "▶ $*"; }
ok()   { echo "  ✓ $*"; }
warn() { echo "  ⚠ $*"; }
err()  { echo "  ✗ $*" >&2; exit 1; }

usage() {
    cat <<'USAGE'
Usage: seed-courseware.sh [--openedx tutor|none] [--data <path>]

  --openedx tutor  create the courses in the local tutor instance (default)
  --openedx none   mitxonline only; use in openedx_mode "qa", where there is
                   no local Open edX to create them in
  --data <path>    seed file to read (default: local-dev/data/courseware-seed.json)
USAGE
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --openedx)
            OPENEDX_MODE="$2"
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

log "Done. Catalog: https://mitxonline.${ROOT_DOMAIN}/catalog/"
# The detail pages are addressed by readable_id, not by the page slug — the
# index pages route on it — so print one rather than leaving it to be guessed.
FIRST_COURSE=$(python3 -c '
import json
import sys

with open(sys.argv[1]) as handle:
    print(json.load(handle)["courses"][0]["readable_id"])
' "${DATA_FILE}" 2>/dev/null || true)
[[ -n "${FIRST_COURSE}" ]] \
    && log "A course page: https://mitxonline.${ROOT_DOMAIN}/courses/${FIRST_COURSE}/"
log "Checkout without a payment gateway: apply the LOCALDEV100 discount code."
