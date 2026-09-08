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

import json
import os
import re
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
]

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
]

RESOURCES = {
    "contract-utilization": CONTRACT_UTILIZATION,
    "enrollment-funnel": ENROLLMENT_FUNNEL,
    "engagement-trend": ENGAGEMENT_TREND,
    "program-funnel": PROGRAM_FUNNEL,
    "content-engagement": CONTENT_ENGAGEMENT,
}

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


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send_json(self, status, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
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
        rows = _rows_for(resource, contract_scoped)
        if rows is None:
            self._send_json(
                404,
                {"detail": f"Unknown analytics resource: {resource}"},
            )
            return

        # Honor LIMIT/OFFSET paging the way the real API does.
        qs = parse_qs(parsed.query)
        try:
            offset = int(qs.get("offset", ["0"])[0])
            limit_raw = qs.get("limit", [None])[0]
            limit = int(limit_raw) if limit_raw is not None else None
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
