#!/usr/bin/env python3
"""Local-dev stub for `ol-analytics-api` (the B2B analytics gateway).

The real service (mitodl/ol-analytics-api) is a FastAPI app that reads
dbt-materialized views out of StarRocks. Neither StarRocks nor the data
pipeline is realistic to run in the k3d local-dev stack, so this stub stands in
for it: it serves the exact endpoints and response envelope the mit-learn
frontend's b2b_dashboard client expects (see mit-learn
`frontends/api/src/analytics/{clients,types}.ts`) with plausible fixture data,
so the dashboard renders instead of reporting itself unavailable.

Deliberately zero-dependency (stdlib `http.server` only) so the pod starts
instantly with no PyPI/registry access and survives cluster restarts.

Auth is handled entirely by APISIX in front of this service (the same
`openid-connect` plugin the other authed routes use). This stub does not verify
anything — it just echoes any `X-Userinfo` it receives for debugging and always
returns data for whatever organization UUID is in the path. The real API keys
every endpoint on the Keycloak organization UUID (`sso_organization_id`); the
stub ignores which UUID it is and returns the same fixtures for all of them.
"""

import datetime
import json
import os
import re
from collections import defaultdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

PORT = int(os.environ.get("PORT", "8070"))

# Path shape after APISIX strips the `/analytics/` prefix:
#   /api/v1/analytics/organizations/<org_uuid>/<resource>
#   /api/v1/analytics/organizations/<org_uuid>/contracts/<contract_id>/<resource>
# The contract-scoped route was added later (mit-learn's org dashboard nests
# analytics under a contract now); CONTRACT_PATH is tried first since its
# extra path segment means ORG_PATH's anchored pattern never matches it.
ORG_PATH = re.compile(
    r"^/api/v1/analytics/organizations/(?P<org>[^/]+)/(?P<resource>[^/?]+)/?$"
)
CONTRACT_PATH = re.compile(
    r"^/api/v1/analytics/organizations/(?P<org>[^/]+)/contracts/(?P<contract>[^/]+)/(?P<resource>[^/?]+)/?$"
)

# A fixed "last refresh" timestamp per section. Static so the stub is
# deterministic (the runtime forbids wall-clock reads in some contexts, and a
# frozen value keeps snapshot tests / screenshots stable).
AS_OF = "2026-07-01T00:00:00Z"

ORG_KEY = "b2b-org-localdev"
ORG_NAME = "Local Dev Organization"

# --- Fixtures: one list per resource, columns matching mit-learn types.ts. ---

CONTRACT_UTILIZATION = [
    {
        "organization_key": ORG_KEY,
        "organization_name": ORG_NAME,
        "contract_pk": "1",
        "contract_id": "1",
        "b2b_contract_name": "FY26 Site License — Data Science Track",
        "b2b_contract_is_active": True,
        "b2b_contract_start_date": "2025-09-01",
        "b2b_contract_end_date": "2026-08-31",
        "seat_limit": 250,
        "b2b_contract_membership_type": "seat_limited",
        "seats_consumed": 187,
        "active_learners": 142,
        "learners_certified": 61,
        "seat_utilization_pct": 74.8,
        "completion_rate_pct": 42.9,
    },
    {
        "organization_key": ORG_KEY,
        "organization_name": ORG_NAME,
        "contract_pk": "2",
        "contract_id": "2",
        "b2b_contract_name": "FY26 Site License — Leadership Track",
        "b2b_contract_is_active": True,
        "b2b_contract_start_date": "2025-09-01",
        "b2b_contract_end_date": "2026-08-31",
        "seat_limit": 100,
        "b2b_contract_membership_type": "seat_limited",
        "seats_consumed": 54,
        "active_learners": 38,
        # k-anonymity floor: a small certified count is suppressed to null.
        "learners_certified": None,
        "seat_utilization_pct": 54.0,
        "completion_rate_pct": None,
    },
]

# Ten more runs on contract "1", so the learner grid has a realistic number of
# module columns to scroll through. Titles and ids are plausible but invented.
# Their funnel and engagement rows are generated below rather than hand-written,
# since `_reconcile_course_counts` overwrites the learner-derived figures anyway.
_EXTRA_COURSERUNS = [
    (
        106,
        "6.00.1x",
        "2026_Spring",
        "Introduction to Computer Science and Programming Using Python",
        "2026-01-12",
        "2026-04-24",
    ),
    (
        107,
        "6.006x",
        "2026_Spring",
        "Introduction to Algorithms",
        "2026-01-12",
        "2026-05-08",
    ),
    (108, "18.06x", "2026_Spring", "Linear Algebra", "2026-01-26", "2026-05-22"),
    (
        109,
        "6.S191x",
        "2026_Spring",
        "Deep Learning Fundamentals",
        "2026-02-09",
        "2026-05-29",
    ),
    (
        110,
        "6.864x",
        "2026_Spring",
        "Natural Language Processing",
        "2026-02-09",
        "2026-06-05",
    ),
    (111, "6.819x", "2026_Summer", "Computer Vision", "2026-05-18", "2026-08-28"),
    (
        112,
        "6.S978x",
        "2026_Summer",
        "Data Ethics and Responsible AI",
        "2026-05-18",
        "2026-08-07",
    ),
    (113, "15.093x", "2026_Summer", "Optimization Methods", "2026-06-01", "2026-09-04"),
    (
        114,
        "14.320x",
        "2026_Summer",
        "Causal Inference and Experimentation",
        "2026-06-08",
        "2026-09-11",
    ),
    (
        115,
        "15.S08x",
        "2026_Summer",
        "AI Strategy for Leaders",
        "2026-06-15",
        "2026-09-18",
    ),
]


def _extra_run_id(number, term):
    """Return the readable id for one of `_EXTRA_COURSERUNS`."""
    return f"course-v1:MITx+{number}+{term}"


ENROLLMENT_FUNNEL = [
    {
        "organization_key": ORG_KEY,
        "organization_name": ORG_NAME,
        "contract_pk": "1",
        "contract_id": "1",
        "b2b_contract_name": "FY26 Site License — Data Science Track",
        "courserun_pk": 101,
        "courserun_readable_id": "course-v1:MITx+14.310x+2026_Spring",
        "courserun_title": "Data Analysis for Social Scientists",
        "enrolled_learners": 96,
        "active_learners": 81,
        "passing_learners": 44,
        "certified_learners": 39,
        "active_rate_pct": 84.4,
        "completion_rate_pct": 45.8,
    },
    {
        "organization_key": ORG_KEY,
        "organization_name": ORG_NAME,
        "contract_pk": "1",
        "contract_id": "1",
        "b2b_contract_name": "FY26 Site License — Data Science Track",
        "courserun_pk": 102,
        "courserun_readable_id": "course-v1:MITx+6.86x+2026_Spring",
        "courserun_title": "Machine Learning with Python",
        "enrolled_learners": 91,
        "active_learners": 67,
        "passing_learners": 22,
        "certified_learners": 22,
        "active_rate_pct": 73.6,
        "completion_rate_pct": 24.2,
    },
    {
        "organization_key": ORG_KEY,
        "organization_name": ORG_NAME,
        "contract_pk": "2",
        "contract_id": "2",
        "b2b_contract_name": "FY26 Site License — Leadership Track",
        "courserun_pk": 201,
        "courserun_readable_id": "course-v1:MITx+15.031x+2026_Spring",
        "courserun_title": "Energy Economics and Policy",
        "enrolled_learners": 54,
        "active_learners": 38,
        "passing_learners": None,
        "certified_learners": None,
        "active_rate_pct": 70.4,
        "completion_rate_pct": None,
    },
    {
        "organization_key": ORG_KEY,
        "organization_name": ORG_NAME,
        "contract_pk": "1",
        "contract_id": "1",
        "b2b_contract_name": "FY26 Site License — Data Science Track",
        "courserun_pk": 103,
        "courserun_readable_id": "course-v1:MITx+6.431x+2026_Spring",
        "courserun_title": "Probability — The Science of Uncertainty and Data",
        # Every count below is overwritten by `_reconcile_course_counts` from
        # the LEARNER_PROGRESS rows for this run; they are placeholders only.
        "enrolled_learners": 0,
        "active_learners": 0,
        "passing_learners": 0,
        "certified_learners": 0,
        "active_rate_pct": 0.0,
        "completion_rate_pct": 0.0,
    },
    {
        "organization_key": ORG_KEY,
        "organization_name": ORG_NAME,
        "contract_pk": "1",
        "contract_id": "1",
        "b2b_contract_name": "FY26 Site License — Data Science Track",
        "courserun_pk": 104,
        "courserun_readable_id": "course-v1:MITx+18.6501x+2026_Spring",
        "courserun_title": "Fundamentals of Statistics",
        # Every count below is overwritten by `_reconcile_course_counts` from
        # the LEARNER_PROGRESS rows for this run; they are placeholders only.
        "enrolled_learners": 0,
        "active_learners": 0,
        "passing_learners": 0,
        "certified_learners": 0,
        "active_rate_pct": 0.0,
        "completion_rate_pct": 0.0,
    },
    {
        "organization_key": ORG_KEY,
        "organization_name": ORG_NAME,
        "contract_pk": "1",
        "contract_id": "1",
        "b2b_contract_name": "FY26 Site License — Data Science Track",
        "courserun_pk": 105,
        "courserun_readable_id": "course-v1:MITx+15.071x+2026_Summer",
        "courserun_title": "The Analytics Edge",
        # Every count below is overwritten by `_reconcile_course_counts` from
        # the LEARNER_PROGRESS rows for this run; they are placeholders only.
        "enrolled_learners": 0,
        "active_learners": 0,
        "passing_learners": 0,
        "certified_learners": 0,
        "active_rate_pct": 0.0,
        "completion_rate_pct": 0.0,
    },
]

ENROLLMENT_FUNNEL.extend(
    {
        "organization_key": ORG_KEY,
        "organization_name": ORG_NAME,
        "contract_pk": "1",
        "contract_id": "1",
        "b2b_contract_name": "FY26 Site License — Data Science Track",
        "courserun_pk": pk,
        "courserun_readable_id": _extra_run_id(number, term),
        "courserun_title": title,
        # Overwritten by `_reconcile_course_counts`; placeholders only.
        "enrolled_learners": 0,
        "active_learners": 0,
        "passing_learners": 0,
        "certified_learners": 0,
        "active_rate_pct": 0.0,
        "completion_rate_pct": 0.0,
    }
    for (pk, number, term, title, _start, _end) in _EXTRA_COURSERUNS
)

ENGAGEMENT_TREND = [
    {
        "organization_key": ORG_KEY,
        "organization_name": ORG_NAME,
        "activity_year_and_month": ym,
        "monthly_active_learners": mal,
        "new_enrollments": newe,
        "certificates_earned": certs,
        "total_videos_watched": vids,
        "total_problems_attempted": probs,
        "total_chatbot_interactions": chats,
    }
    for (ym, mal, newe, certs, vids, probs, chats) in [
        ("2025-09", 41, 41, None, 512, 883, 120),
        ("2025-10", 88, 52, None, 1340, 2110, 402),
        ("2025-11", 121, 39, 12, 2015, 3488, 690),
        ("2025-12", 104, 18, 21, 1789, 2901, 548),
        ("2026-01", 133, 44, 17, 2450, 4102, 812),
        ("2026-02", 142, 22, 33, 2688, 4530, 905),
    ]
]

PROGRAM_FUNNEL = [
    {
        "organization_key": ORG_KEY,
        "organization_name": ORG_NAME,
        "contract_pk": "1",
        "contract_id": "1",
        "b2b_contract_name": "FY26 Site License — Data Science Track",
        "program_pk": 10,
        "program_title": "Statistics and Data Science MicroMasters",
        "total_courses": 5,
        "enrolled_in_contract_courses": 96,
        "enrolled_via_program": 71,
        "program_course_completers": 33,
    },
    {
        "organization_key": ORG_KEY,
        "organization_name": ORG_NAME,
        "contract_pk": "2",
        "contract_id": "2",
        "b2b_contract_name": "FY26 Site License — Leadership Track",
        "program_pk": 20,
        "program_title": "Sustainability Leadership Professional Certificate",
        "total_courses": 3,
        "enrolled_in_contract_courses": 54,
        "enrolled_via_program": None,
        "program_course_completers": None,
    },
]

CONTENT_ENGAGEMENT = [
    {
        "organization_key": ORG_KEY,
        "organization_name": ORG_NAME,
        "courserun_readable_id": "course-v1:MITx+14.310x+2026_Spring",
        "courserun_title": "Data Analysis for Social Scientists",
        "total_enrolled_learners": 96,
        "engaged_learners": 81,
        "engagement_rate_pct": 84.4,
        "total_videos_watched": 4120,
        "avg_videos_per_engaged_learner": 50.9,
        "total_problems_attempted": 6890,
        "avg_problems_per_engaged_learner": 85.1,
        "total_chatbot_interactions": 1830,
        "chatbot_users": 58,
        "chatbot_adoption_pct": 71.6,
        "certificates_earned": 39,
    },
    {
        "organization_key": ORG_KEY,
        "organization_name": ORG_NAME,
        "courserun_readable_id": "course-v1:MITx+6.86x+2026_Spring",
        "courserun_title": "Machine Learning with Python",
        "total_enrolled_learners": 91,
        "engaged_learners": 67,
        "engagement_rate_pct": 73.6,
        "total_videos_watched": 3980,
        "avg_videos_per_engaged_learner": 59.4,
        "total_problems_attempted": 7210,
        "avg_problems_per_engaged_learner": 107.6,
        "total_chatbot_interactions": 2405,
        "chatbot_users": 51,
        "chatbot_adoption_pct": 76.1,
        "certificates_earned": 22,
    },
    {
        "organization_key": ORG_KEY,
        "organization_name": ORG_NAME,
        "courserun_readable_id": "course-v1:MITx+15.031x+2026_Spring",
        "courserun_title": "Energy Economics and Policy",
        "total_enrolled_learners": 54,
        "engaged_learners": None,
        "engagement_rate_pct": None,
        "total_videos_watched": None,
        "avg_videos_per_engaged_learner": None,
        "total_problems_attempted": None,
        "avg_problems_per_engaged_learner": None,
        "total_chatbot_interactions": None,
        "chatbot_users": None,
        "chatbot_adoption_pct": None,
        "certificates_earned": None,
    },
    {
        "organization_key": ORG_KEY,
        "organization_name": ORG_NAME,
        "courserun_readable_id": "course-v1:MITx+6.431x+2026_Spring",
        "courserun_title": "Probability — The Science of Uncertainty and Data",
        # total_enrolled_learners and certificates_earned are recomputed by
        # `_reconcile_course_counts`; the engagement figures have no
        # LEARNER_PROGRESS equivalent, so they stay hand-authored.
        "total_enrolled_learners": 0,
        "engaged_learners": 74,
        "engagement_rate_pct": 79.6,
        "total_videos_watched": 3610,
        "avg_videos_per_engaged_learner": 48.8,
        "total_problems_attempted": 8120,
        "avg_problems_per_engaged_learner": 109.7,
        "total_chatbot_interactions": 2110,
        "chatbot_users": 55,
        "chatbot_adoption_pct": 74.3,
        "certificates_earned": 0,
    },
    {
        "organization_key": ORG_KEY,
        "organization_name": ORG_NAME,
        "courserun_readable_id": "course-v1:MITx+18.6501x+2026_Spring",
        "courserun_title": "Fundamentals of Statistics",
        # total_enrolled_learners and certificates_earned are recomputed by
        # `_reconcile_course_counts`; the engagement figures have no
        # LEARNER_PROGRESS equivalent, so they stay hand-authored.
        "total_enrolled_learners": 0,
        "engaged_learners": 62,
        "engagement_rate_pct": 70.5,
        "total_videos_watched": 2980,
        "avg_videos_per_engaged_learner": 48.1,
        "total_problems_attempted": 7460,
        "avg_problems_per_engaged_learner": 120.3,
        "total_chatbot_interactions": 1620,
        "chatbot_users": 44,
        "chatbot_adoption_pct": 71.0,
        "certificates_earned": 0,
    },
    {
        "organization_key": ORG_KEY,
        "organization_name": ORG_NAME,
        "courserun_readable_id": "course-v1:MITx+15.071x+2026_Summer",
        "courserun_title": "The Analytics Edge",
        # total_enrolled_learners and certificates_earned are recomputed by
        # `_reconcile_course_counts`; the engagement figures have no
        # LEARNER_PROGRESS equivalent, so they stay hand-authored.
        "total_enrolled_learners": 0,
        "engaged_learners": 45,
        "engagement_rate_pct": 58.4,
        "total_videos_watched": 1740,
        "avg_videos_per_engaged_learner": 38.7,
        "total_problems_attempted": 3210,
        "avg_problems_per_engaged_learner": 71.3,
        "total_chatbot_interactions": 940,
        "chatbot_users": 31,
        "chatbot_adoption_pct": 68.9,
        "certificates_earned": 0,
    },
]

CONTENT_ENGAGEMENT.extend(
    {
        "organization_key": ORG_KEY,
        "organization_name": ORG_NAME,
        "courserun_readable_id": _extra_run_id(number, term),
        "courserun_title": title,
        # Recomputed by `_reconcile_course_counts`; the engagement figures have
        # no LEARNER_PROGRESS equivalent, so they are varied by position.
        "total_enrolled_learners": 0,
        "engaged_learners": 40 + (i * 7) % 35,
        "engagement_rate_pct": round(60 + (i * 5) % 25, 1),
        "total_videos_watched": 1500 + i * 230,
        "avg_videos_per_engaged_learner": round(35 + (i * 3) % 20, 1),
        "total_problems_attempted": 3000 + i * 410,
        "avg_problems_per_engaged_learner": round(70 + (i * 11) % 50, 1),
        "total_chatbot_interactions": 800 + i * 120,
        "chatbot_users": 25 + (i * 4) % 25,
        "chatbot_adoption_pct": round(62 + (i * 3) % 15, 1),
        "certificates_earned": 0,
    }
    for i, (_pk, number, term, title, _start, _end) in enumerate(_EXTRA_COURSERUNS)
)

RESOURCES = {
    "contract-utilization": CONTRACT_UTILIZATION,
    "enrollment-funnel": ENROLLMENT_FUNNEL,
    "engagement-trend": ENGAGEMENT_TREND,
    "program-funnel": PROGRAM_FUNNEL,
    "content-engagement": CONTENT_ENGAGEMENT,
}

# learner-progress is handled separately from RESOURCES: it is the one
# individual-learner endpoint (see mit-learn's LearnerProgress type), so it
# takes its own filters (search, completion_status, consent) and its own
# envelope field (outcomes_withheld_count) rather than the aggregate
# resources' shared offset/limit-only handling.
#
# Every course run on the contract, which is what `course-runs` serves and
# what the frontend's module filter lists. Read from `mv_b2b_contract_courserun`
# in the real API: keyed on the CONTRACT, so it is neither consent-gated nor
# limited to runs someone has enrolled in. That distinction is the whole reason
# the endpoint exists rather than the filter deriving its options from
# enrollment rows, so the fixture has to be able to express it — hence the
# split from `_ENROLLED_COURSERUNS` below.
# Annotated because the unopened run's `courserun_end_on` is None while every
# other value is a str: dict is invariant in its value type, so mypy otherwise
# joins the entries to `object` and every `run[...]` read below fails to check.
_CONTRACT_COURSERUNS: list[dict[str, str | None]] = [
    {
        "courserun_readable_id": "course-v1:MITx+14.310x+2026_Spring",
        "courserun_title": "Data Analysis for Social Scientists",
        "courserun_start_on": "2026-01-05T00:00:00Z",
        "courserun_end_on": "2026-05-15T00:00:00Z",
    },
    {
        "courserun_readable_id": "course-v1:MITx+6.86x+2026_Spring",
        "courserun_title": "Machine Learning with Python",
        "courserun_start_on": "2026-01-05T00:00:00Z",
        "courserun_end_on": "2026-05-15T00:00:00Z",
    },
    {
        "courserun_readable_id": "course-v1:MITx+6.431x+2026_Spring",
        "courserun_title": "Probability — The Science of Uncertainty and Data",
        "courserun_start_on": "2026-01-05T00:00:00Z",
        "courserun_end_on": "2026-05-15T00:00:00Z",
    },
    {
        "courserun_readable_id": "course-v1:MITx+18.6501x+2026_Spring",
        "courserun_title": "Fundamentals of Statistics",
        "courserun_start_on": "2026-02-02T00:00:00Z",
        "courserun_end_on": "2026-06-12T00:00:00Z",
    },
    {
        "courserun_readable_id": "course-v1:MITx+15.071x+2026_Summer",
        "courserun_title": "The Analytics Edge",
        "courserun_start_on": "2026-06-01T00:00:00Z",
        "courserun_end_on": "2026-08-21T00:00:00Z",
    },
    *[
        {
            "courserun_readable_id": _extra_run_id(number, term),
            "courserun_title": title,
            "courserun_start_on": f"{start}T00:00:00Z",
            "courserun_end_on": f"{end}T00:00:00Z",
        }
        for (_pk, number, term, title, start, end) in _EXTRA_COURSERUNS
    ],
    # Last in the track and not yet open, so nobody is enrolled: it appears in
    # the module filter (the contract covers it) while matching zero rows in
    # `learner-progress`. Selecting it SHOULD empty the table — that is the
    # correct answer, not a broken query.
    {
        "courserun_readable_id": "course-v1:MITx+6.419x+2026_Fall",
        "courserun_title": "Data Analysis: Statistical Modeling and Computation",
        "courserun_start_on": "2026-09-08T00:00:00Z",
        "courserun_end_on": None,
    },
]

# The runs `learner-progress` generates enrollments for: everything except the
# unopened final module. Every one of these also has an ENROLLMENT_FUNNEL and
# CONTENT_ENGAGEMENT entry, so the dashboard's Course performance section and
# the learner directory agree on the course list (see
# `_reconcile_course_counts`).
_ENROLLED_COURSERUNS = [
    run
    for run in _CONTRACT_COURSERUNS
    if run["courserun_readable_id"] != "course-v1:MITx+6.419x+2026_Fall"
]

_LEARNER_FIRST_NAMES = [
    "Anton",
    "Hugo",
    "Jordan",
    "Marcus",
    "Sam",
    "Nina",
    "Rohan",
    "Tobias",
    "Zara",
    "Grace",
    "Amara",
    "Chiara",
    "Sofia",
    "Aisha",
    "Noah",
    "Yuki",
    "Anja",
    "Luca",
    "Priya",
    "Mateo",
    "Elena",
    "Kwame",
    "Ines",
    "Felix",
]
_LEARNER_LAST_NAMES = [
    "Petrov",
    "Bernard",
    "Brooks",
    "Reid",
    "Okafor",
    "Lang",
    "Malik",
    "Nakamura",
    "Nwosu",
    "Bruno",
    "Rossi",
    "Rahman",
    "Weiss",
    "Tanaka",
    "Kowalski",
    "Gupta",
    "Moreau",
    "Silva",
    "Haddad",
    "Novak",
    "Adeyemi",
    "Cohen",
    "Dubois",
    "Park",
]
_LEARNER_STATUSES = ["not_started", "in_progress", "passed", "certified"]

# Learners with no usable name. Production returns "" (not null) for B2B SSO
# learners who never set a profile name; null and whitespace-only cover the
# other shapes the frontend has to treat as "no name".
_NAMELESS_LEARNERS = [
    (None, "x7k2m@example.edu"),
    ("", "jdoe@example.edu"),
    ("   ", "sso.user.4471@example.edu"),
]


# Anchored to AS_OF rather than the wall clock, so the column is stable across
# pod restarts and screenshots (same reason AS_OF itself is frozen).
_ACTIVITY_ANCHOR = datetime.date(2026, 6, 30)


def _last_active_on(n, status, shared):
    """Return a plain `YYYY-MM-DD`, matching the real field's `format: date`.

    Deliberately NOT a timestamp: `LearnerProgress.last_active_on` is the only
    date-only field on that model, and a stub that served
    `2026-06-30T00:00:00Z` here would let a frontend that formats it via
    `new Date()` look correct locally while rendering the previous day for
    every user west of Greenwich.

    Null in the two cases the real API nulls it: withheld consent (the response
    model blanks every outcome field), and `not_started`, which means no
    recorded activity at all. The spread runs 3 to 77 days back so rows land on
    both sides of the 30-day needs-attention boundary.
    """
    if not shared or status == "not_started":
        return None
    return (_ACTIVITY_ANCHOR - datetime.timedelta(days=3 + (n * 17) % 75)).isoformat()


# Mirrors the real API's NEEDS_ATTENTION_QUIET_DAYS. There the cutoff is read
# from StarRocks once per request so two round trips cannot straddle midnight;
# here _ACTIVITY_ANCHOR is already frozen, so one constant does the same job.
NEEDS_ATTENTION_QUIET_DAYS = 30
_NEEDS_ATTENTION_CUTOFF = _ACTIVITY_ANCHOR - datetime.timedelta(
    days=NEEDS_ATTENTION_QUIET_DAYS
)


def _needs_attention(status, last_active_on, shared):
    """Port of `learner_queries._needs_attention`: never started, or still
    in progress with last recorded activity on or before the cutoff.

    Staleness is scoped to `in_progress`, as in the real expression: going
    quiet after passing or certifying is the expected end of a course, not
    something a manager should chase.

    `<=` is deliberate — "at least 30 days ago" includes the 30th day itself.

    None when consent is withheld, matching every other outcome field. On a
    shared row this is always a bool, never None: a row with a grade but no
    tracked activity has no timestamp to judge quiet against, and the real
    expression COALESCEs that to false rather than leaving it NULL. That is
    what keeps such a row from falling out of `needs_attention=true` and
    `needs_attention=false` alike.
    """
    if not shared:
        return None
    if status == "not_started":
        return True
    if status != "in_progress" or last_active_on is None:
        return False
    return datetime.date.fromisoformat(last_active_on) <= _NEEDS_ATTENTION_CUTOFF


# One run in this many is skipped for each learner (see `_build_learner_progress`).
_SKIP_EVERY = 4


def _build_learner_progress():
    """Deterministic so the stub's fixture is stable across pod restarts.

    Roughly one enrollment in nine has withheld consent (outcomes_shared
    False, every outcome field null) and one in twelve is deactivated, so
    both the consent banner and `include_inactive` have something to show
    without any per-request randomness.
    """
    named = []
    for i, first in enumerate(_LEARNER_FIRST_NAMES):
        last = _LEARNER_LAST_NAMES[(i * 7) % len(_LEARNER_LAST_NAMES)]
        named.append((f"{first} {last}", f"{first.lower()}.{last.lower()}@example.edu"))

    rows = []
    for i, (full_name, email) in enumerate(named + _NAMELESS_LEARNERS):
        for run_index, run in enumerate(_ENROLLED_COURSERUNS):
            n = i * len(_ENROLLED_COURSERUNS) + run_index
            # About a quarter of the runs are skipped per learner, so the grid
            # has "not enrolled" cells and not just status variety.
            if (i * 3 + run_index) % _SKIP_EVERY == _SKIP_EVERY - 1:
                continue
            shared = n % 9 != 0
            status = _LEARNER_STATUSES[n % len(_LEARNER_STATUSES)]
            certified = status == "certified"
            rows.append(
                {
                    "learner_id": f"kc-{1000 + n}",
                    "email": email,
                    "full_name": full_name,
                    "courserun_readable_id": run["courserun_readable_id"],
                    "courserun_title": run["courserun_title"],
                    "courserun_start_on": run["courserun_start_on"],
                    "courserun_end_on": run["courserun_end_on"],
                    "enrolled_on": "2026-01-12T00:00:00Z",
                    "enrollment_is_active": n % 12 != 0,
                    "enrollment_mode": "audit" if n % 4 == 0 else "verified",
                    "outcomes_shared": shared,
                    "completion_status": status if shared else None,
                    "is_passing": (status in ("passed", "certified"))
                    if shared
                    else None,
                    "grade": round((n % 10) / 10, 2) if shared else None,
                    "letter_grade": None,
                    "certificate_issued_on": "2026-06-01T00:00:00Z"
                    if shared and certified
                    else None,
                    "certificate_is_revoked": False if shared and certified else None,
                    "last_active_on": _last_active_on(n, status, shared),
                    "needs_attention": _needs_attention(
                        status, _last_active_on(n, status, shared), shared
                    ),
                }
            )
    return rows


LEARNER_PROGRESS = _build_learner_progress()


def _reconcile_course_counts():
    """Keeps `ENROLLMENT_FUNNEL`/`CONTENT_ENGAGEMENT`'s per-course "enrolled"
    figures consistent with `LEARNER_PROGRESS` for the courses both cover,
    rather than leaving two independently hand-authored numbers free to drift
    apart. Mit-learn's Learner progress section reads one, Course
    performance/Content engagement read the other, side by side on the same
    page, so a mismatch there reads as a bug rather than a stub quirk.

    The course under contract "2" has no `LEARNER_PROGRESS` rows at all (that
    fixture only covers contract "1" — see `_VIEWED_CONTRACT`), so it's left
    as hand-authored fixture data untouched.
    """
    by_course = defaultdict(list)
    for row in LEARNER_PROGRESS:
        by_course[row["courserun_readable_id"]].append(row)

    for entry in ENROLLMENT_FUNNEL:
        rows = by_course.get(entry["courserun_readable_id"])
        if not rows:
            continue
        enrolled = len(rows)
        active = sum(1 for r in rows if r["enrollment_is_active"])
        passing = sum(
            1
            for r in rows
            if r["outcomes_shared"]
            and r["completion_status"] in ("passed", "certified")
        )
        certified = sum(
            1
            for r in rows
            if r["outcomes_shared"] and r["completion_status"] == "certified"
        )
        entry.update(
            {
                "enrolled_learners": enrolled,
                "active_learners": active,
                "passing_learners": passing,
                "certified_learners": certified,
                "active_rate_pct": round(active / enrolled * 100, 1),
                "completion_rate_pct": round(passing / enrolled * 100, 1),
            }
        )

    for entry in CONTENT_ENGAGEMENT:
        rows = by_course.get(entry["courserun_readable_id"])
        if not rows:
            continue
        entry["total_enrolled_learners"] = len(rows)
        entry["certificates_earned"] = sum(
            1
            for r in rows
            if r["outcomes_shared"] and r["completion_status"] == "certified"
        )


_reconcile_course_counts()

# `course-runs` rows. The readable id is served under `courserun_id`, NOT
# `courserun_readable_id`: the real query is
# `SELECT courserun_readable_id AS courserun_id`, and the frontend reads
# `courserun_id` and sends it back as the `courserun_readable_id` filter. A
# stub that "helpfully" used the same name at both ends would hide a wiring
# bug that only shows up against the real service.
COURSE_RUNS = [
    {
        "courserun_id": run["courserun_readable_id"],
        "courserun_title": run["courserun_title"],
        "courserun_start_on": run["courserun_start_on"],
        "courserun_end_on": run["courserun_end_on"],
    }
    for run in _CONTRACT_COURSERUNS
]

# Identity of "the" contract every contract-scoped request is treated as
# viewing — the stub ignores which contract_id is actually in the path (same
# policy as the org UUID) and always answers as contract "1".
_VIEWED_CONTRACT = {
    "contract_pk": "1",
    "contract_id": "1",
    "b2b_contract_name": "FY26 Site License — Data Science Track",
}


def _rows_for(resource, contract_scoped):
    """Rows for `resource`, scoped to the org or to `_VIEWED_CONTRACT`.

    Org-scoped calls return every fixture row unfiltered (mixed contracts),
    matching the real org-level views. Contract-scoped calls narrow to just
    `_VIEWED_CONTRACT`: for the three views that already carry contract
    identity at every grain, that's a filter; `engagement-trend` and
    `content-engagement` have no contract-scoped fixture rows of their own
    (they're org x month / org x run in this stub), so the same rows are
    reused with the contract identity fields stamped on — matching
    `ContractMonthlyEngagementTrend`/`ContractContentEngagementDepth`, which
    are just the org row types plus `ContractIdentity`.
    """
    rows = RESOURCES.get(resource)
    if rows is None:
        return None
    if not contract_scoped:
        return rows
    if resource in ("engagement-trend", "content-engagement"):
        return [dict(row, **_VIEWED_CONTRACT) for row in rows]
    return [
        row for row in rows if row.get("contract_pk") == _VIEWED_CONTRACT["contract_pk"]
    ]


def _needs_attention_row():
    """`ContractNeedsAttention` for `_VIEWED_CONTRACT`, derived from
    `LEARNER_PROGRESS` so it always agrees with the learner directory's
    `needs_attention` filter, as the real query-time aggregate does.

    Counts distinct learners, not enrollments: a learner behind in two runs
    counts once. Keyed on email because the fixture's `learner_id` is unique
    per row rather than per learner. Active enrollments only, matching the
    real endpoint and `learner-progress`'s `include_inactive=false` default.
    Not floored: the fixture's counts sit well above the real API's floor.
    """
    active = [r for r in LEARNER_PROGRESS if r["enrollment_is_active"]]
    return {
        "contract_id": int(_VIEWED_CONTRACT["contract_id"]),
        "learners_considered": len({r["email"] for r in active}),
        "learners_needing_attention": len(
            {r["email"] for r in active if r["needs_attention"]}
        ),
        "learners_outcomes_withheld": len(
            {r["email"] for r in active if not r["outcomes_shared"]}
        ),
    }


# Endpoints the real API defines only under the contract prefix, each returning
# individual rows rather than an aggregate, so each has its own handler instead
# of going through RESOURCES.
CONTRACT_ONLY = {
    "learner-progress": "_handle_learner_progress",
    "course-runs": "_handle_course_runs",
}

# Computed endpoints served at both scopes. The org form returns one row per
# contract; the stub only has learner rows for `_VIEWED_CONTRACT`, so both
# return that single row.
COMPUTED = {
    "needs-attention": "_handle_needs_attention",
}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send_json(self, status, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    @staticmethod
    def _parse_offset_limit(qs):
        offset = int(qs.get("offset", ["0"])[0])
        limit_raw = qs.get("limit", [None])[0]
        limit = int(limit_raw) if limit_raw is not None else None
        return offset, limit

    def _handle_course_runs(self, org, qs):
        """course-runs: the module filter's options.

        Paged but never filtered: the real endpoint takes only limit/offset.
        """
        offset, limit = self._parse_offset_limit(qs)
        page = COURSE_RUNS[offset:]
        if limit is not None:
            page = page[:limit]
        self._send_json(
            200,
            {
                "organization_id": org,
                "as_of": AS_OF,
                "total_count": len(COURSE_RUNS),
                "data": page,
            },
        )

    def _handle_needs_attention(self, org, qs):
        """needs-attention: distinct learners needing attention, paged like
        every org envelope even though there is only ever one row here.
        """
        rows = [_needs_attention_row()]
        offset, limit = self._parse_offset_limit(qs)
        page = rows[offset:]
        if limit is not None:
            page = page[:limit]
        self._send_json(
            200,
            {
                "organization_id": org,
                "as_of": AS_OF,
                "total_count": len(rows),
                "data": page,
            },
        )

    def _handle_learner_progress(self, org, qs):
        """learner-progress: filter, sort, then page — in that order, so
        `total_count`/`outcomes_withheld_count` reflect the filtered set and
        not the fixture's full row set.
        """
        search = (qs.get("search", [""])[0] or "").strip().lower()
        statuses = qs.get("completion_status", [])
        include_inactive = qs.get("include_inactive", ["false"])[0].lower() == "true"
        courserun = qs.get("courserun_readable_id", [None])[0]
        needs_attention = qs.get("needs_attention", [None])[0]
        sort_key = qs.get("sort", ["full_name"])[0]
        descending = qs.get("descending", ["false"])[0].lower() == "true"

        rows = LEARNER_PROGRESS
        if not include_inactive:
            rows = [r for r in rows if r["enrollment_is_active"]]
        if courserun:
            rows = [r for r in rows if r["courserun_readable_id"] == courserun]
        if search:
            rows = [
                r
                for r in rows
                if search in (r["full_name"] or "").lower()
                or search in r["email"].lower()
            ]
        if statuses:
            rows = [
                r
                for r in rows
                if (r["completion_status"] in statuses)
                or (r["completion_status"] is None and "unknown" in statuses)
            ]
        if needs_attention is not None:
            # Withheld rows carry None here and so match NEITHER true nor
            # false, exactly as the real API's two-valued SQL predicate leaves
            # them out of both. Unlike `completion_status` there is no
            # `unknown` escape hatch, so omitting the param is the only way to
            # see them.
            wanted = needs_attention.lower() == "true"
            rows = [r for r in rows if r["needs_attention"] is wanted]

        if sort_key in ("full_name", "email", "enrolled_on", "courserun_readable_id"):
            # Same as the real API's `<key> IS NULL, <key> <dir>`: nulls last in
            # either direction, while "" and "   " sort ahead of every name.
            present = [r for r in rows if r[sort_key] is not None]
            missing = [r for r in rows if r[sort_key] is None]
            rows = [
                *sorted(present, key=lambda r: r[sort_key], reverse=descending),
                *missing,
            ]

        total_count = len(rows)
        outcomes_withheld_count = sum(1 for r in rows if not r["outcomes_shared"])
        # Not floored by k-anonymity in the real API either (waived for this
        # endpoint per mitodl/ol-analytics-api#58), so this tallies plainly.
        # Sums to slightly less than total_count here because it's tallied off
        # the already-consent-nulled `completion_status` field, unlike the real
        # API, which tallies before nulling and always sums to total_count.
        completion_status_counts = {
            status: sum(1 for r in rows if r["completion_status"] == status)
            for status in _LEARNER_STATUSES
        }
        # Overlapping, not a fifth bucket: a row can be both `in_progress` and
        # needing attention, so this does not partition the counts above it.
        # Withheld rows are excluded, same as the filter.
        needs_attention_count = sum(1 for r in rows if r["needs_attention"])

        offset, limit = self._parse_offset_limit(qs)
        page = rows[offset:]
        if limit is not None:
            page = page[:limit]

        self._send_json(
            200,
            {
                "organization_id": org,
                "as_of": AS_OF,
                "total_count": total_count,
                "outcomes_withheld_count": outcomes_withheld_count,
                "completion_status_counts": completion_status_counts,
                "needs_attention_count": needs_attention_count,
                "data": page,
            },
        )

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        # Health / readiness probe.
        if path in ("/", "/health", "/healthz"):
            self._send_json(200, {"status": "ok", "service": "analytics-api-stub"})
            return

        match = CONTRACT_PATH.match(path)
        contract_scoped = match is not None
        if not match:
            match = ORG_PATH.match(path)
        if not match:
            self._send_json(404, {"detail": f"Not found: {path}"})
            return

        org = match.group("org")
        resource = match.group("resource")
        qs = parse_qs(parsed.query)

        handler = COMPUTED.get(resource) or CONTRACT_ONLY.get(resource)
        if handler:
            if resource in CONTRACT_ONLY and not contract_scoped:
                self._send_json(404, {"detail": f"{resource} is contract-scoped only"})
                return
            try:
                getattr(self, handler)(org, qs)
            except ValueError:
                self._send_json(422, {"detail": "limit/offset must be integers"})
            return

        rows = _rows_for(resource, contract_scoped)
        if rows is None:
            self._send_json(
                404,
                {"detail": f"Unknown analytics resource: {resource}"},
            )
            return

        # Honor LIMIT/OFFSET paging the way the real API does.
        try:
            offset, limit = self._parse_offset_limit(qs)
        except ValueError:
            self._send_json(422, {"detail": "limit/offset must be integers"})
            return

        page = rows[offset:]
        if limit is not None:
            page = page[:limit]

        self._send_json(
            200,
            {
                "organization_id": org,
                "as_of": AS_OF,
                # Total rows for this resource, ignoring limit/offset paging.
                # The frontend renders `total_count.toLocaleString()`, so it
                # must always be present (never null/undefined).
                "total_count": len(rows),
                "data": page,
            },
        )

    def log_message(self, fmt, *args):  # keep pod logs readable
        userinfo = self.headers.get("X-Userinfo", "-")
        print(
            "analytics-api-stub %s - %s"
            % (self.command + " " + self.path, "auth" if userinfo != "-" else "anon")
        )


def main():
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"analytics-api stub listening on :{PORT}")
    server.serve_forever()


if __name__ == "__main__":
    main()
