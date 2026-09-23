"""User journeys: the request fan-out behind one user-facing capability.

A journey names the HTTP endpoints a single product capability actually calls,
in the order the browser calls them, so a dashboard can show that capability's
performance as one thing rather than as scattered rows on a per-service RED
dashboard. `user_journey.py` renders one dashboard per entry in `JOURNEYS`.

Adding another capability means appending a `Journey` here -- no new module and
no change to the renderer.

WHERE THE ENDPOINT STRINGS COME FROM: `step.target` must equal the
`http_target` label the OTel wsgi/asgi instrumentation puts on
`http_server_duration_milliseconds`, which is the Django URL *pattern*, not a
request path (see service_red.py for why `http_target` and not `http_route`).
The patterns below were read off the live production metric on 2026-09-17
rather than derived from the URLconf, because the two differ in ways that are
easy to get wrong -- mit-learn's user endpoint is `^api/v0/users/me/$` while
MITx Online's is `api/v0/users/me`, with neither the anchors nor the trailing
slash matching.
"""

from ol_infrastructure.infrastructure.grafana_alerting.dashboards.user_journey import (
    Journey,
    JourneyStep,
)

LEARN_WEBAPP = "learn-webapp"
MITXONLINE_WEBAPP = "mitxonline-webapp"

MIT_LEARN_ORGANIZATION_DASHBOARD = Journey(
    uid="journey-mit-learn-organization-dashboard",
    title="User journey - MIT Learn organization dashboard",
    slug="mit-learn-organization-dashboard",
    description=(
        "Every request behind the B2B organization dashboard a learner lands "
        "on after authenticating: mit-learn renders the page, but all of the "
        "course and enrollment data comes from MITx Online."
    ),
    # The comparison row defaults to MITx Online because that is where this
    # journey's expensive work happens -- mit-learn contributes one session
    # lookup, MITx Online contributes eight data calls.
    primary_service=MITXONLINE_WEBAPP,
    focus_step="api/v2/courses/$",
    steps=[
        JourneyStep(
            label="mit-learn session gate",
            service=LEARN_WEBAPP,
            target="^api/v0/users/me/$",
            note=(
                "RestrictedRoute and DashboardLayout both call this before "
                "anything under /dashboard renders, so it gates the route."
            ),
        ),
        JourneyStep(
            label="MITx Online user, orgs and contracts",
            service=MITXONLINE_WEBAPP,
            target="api/v0/users/me",
            note=(
                "Carries b2b_organizations[].contracts[]. "
                "/dashboard/organization/[orgSlug] reads the first contract "
                "slug out of this response to redirect, so the whole journey "
                "is serialised behind it."
            ),
        ),
        JourneyStep(
            label="Contract courses",
            service=MITXONLINE_WEBAPP,
            target="api/v2/courses/$",
            note=(
                "useContractDashboardData requests this with page_size=200, "
                "org_id and contract_id. The widest call in the journey and "
                "the one the pending fix targets."
            ),
        ),
        JourneyStep(
            label="Contract programs",
            service=MITXONLINE_WEBAPP,
            target="api/v2/programs/$",
            note="Requested with page_size=30, scoped to org and contract.",
        ),
        JourneyStep(
            label="Program collections",
            service=MITXONLINE_WEBAPP,
            target="api/v2/program-collections/$",
            note=(
                "Fetched unscoped -- the frontend filters collections down to "
                "the contract's programs client-side."
            ),
        ),
        JourneyStep(
            label="Course run enrollments",
            service=MITXONLINE_WEBAPP,
            target="api/v3/enrollments/$",
            note="The learner's own enrollments, not scoped to the contract.",
        ),
        JourneyStep(
            label="Program enrollments",
            service=MITXONLINE_WEBAPP,
            target="api/v3/program_enrollments/$",
        ),
        JourneyStep(
            label="Variant runs",
            service=MITXONLINE_WEBAPP,
            target="api/v3/courses/variant_runs/",
            note=(
                "Lazy second phase: fires only once the learner picks a "
                "non-default variant, so it sits near zero on the rate panels "
                "and should not be read as a broken step."
            ),
        ),
        JourneyStep(
            label="Manager organization check",
            service=MITXONLINE_WEBAPP,
            target="api/v0/b2b/manager/organizations/$",
            note=(
                "ContractContent calls this to decide whether to show the "
                "contract admin link. Gated: the query is enabled only when "
                "the B2BContractManagerDashboard or B2BAnalyticsDashboard "
                "PostHog flag is on, and then it runs for every viewer rather "
                "than only for managers. With both flags off this step has no "
                "traffic at all, which is correct rather than a broken panel."
            ),
        ),
    ],
    known_gaps=(
        "The Next.js app that serves the page itself emits no "
        "http.server.duration -- `service_name` on that metric covers only "
        "the Django services (checked 2026-09-17: learn-ai-webapp, "
        "learn-webapp, mitxonline-webapp, ocw-studio-webapp, ovs-webapp). "
        "Everything below is API time, so it is a lower bound on what the "
        "learner waits for, missing SSR, bundle transfer and client render."
    ),
)

JOURNEYS = [MIT_LEARN_ORGANIZATION_DASHBOARD]
