# mitxonline canaries

**Property:** MITx Online — <https://mitxonline.mit.edu>
**First target environment:** RC — <https://rc.mitxonline.mit.edu>
**Owner:** MIT Open Learning infrastructure / devops

The first journeys cover the staff dashboard (`/staff-dashboard/`, a Refine/antd app
in `mitodl/mitxonline`'s `frontend/staff-dashboard`). They exist to catch a broken
dashboard bundle or auth flow on RC, e.g. from the Refine 3 to Refine 4 upgrade.

## Journeys

| Spec | Journey | Status |
|---|---|---|
| `staff-dashboard-anonymous.spec.ts` | `/staff-dashboard/` sends an anonymous visitor to sign in; a deep dashboard route loads the app, whose auth check does the same | Active |
| `staff-dashboard-signed-in.spec.ts` | A staff user reaches the dashboard and the Flexible Pricing list | Active |

## Canary account

The signed-in journey uses the **mit-learn canary account**, not one of its own.
Decided 2026-09-18: that account was granted `is_staff` on RC MITx Online, and
`is_superuser` stays false. It already signed in to RC MITx Online, because both
properties authenticate through the same `olapps` realm and `ol-mitlearn-client`
client. So `credential_secret` is `canary_mit_learn` here too. The account's details,
and the note on clearing first-login onboarding, are in `../mit-learn/README.md`.

`is_staff` is set on the MITx Online user record in RC. It is not Keycloak or
Pulumi state, so nothing here recreates it. If RC's database is restored or the user
is recreated, this journey fails at the dashboard heading until the grant is re-run.
The account must never be made superuser. A canary does not need it, and the
discount journeys that would need it are deliberately not written.

The accepted cost of sharing is lockout exposure. Both pipelines submit the same
password every 10 minutes. The rejected-credential marker lives in each run's
container, so it stops a retry within one run and does nothing across pipelines.
A drifted password therefore reaches the realm's `failureFactor=10` twice as fast as
with one pipeline: two rejected submissions per 10 minutes instead of one, so the
"about two hours" to a permanent disable in `../mit-learn/README.md` becomes about
one. Treat a red `canary-mit-learn` or `canary-mitxonline` build that
names a rejected credential as urgent, and pause both pipelines before debugging.

Rotation is still one update. Both pipelines are in the same Concourse team and read
the same credential. `((canary_mit_learn.*))` resolves through Concourse's default
lookup templates to `secret-concourse/infrastructure/canary_mit_learn`, with no
pipeline-scoped copy, and `src/bridge/secrets/concourse/operations.production.yaml`
has the single key `infrastructure/canary_mit_learn`. So a rotation changes Keycloak
and that one SOPS value together, as `../mit-learn/README.md` already requires.
Both pipelines pick the change up.

## What the journeys may do

Both journeys are read-only. The signed-in one navigates but never clicks Create,
Save or a status change, and there are no discount journeys: those need superuser,
which a canary must not hold.

The Flexible Pricing list shows learners' names, email addresses and incomes, and
failure artifacts are published to S3, so the signed-in journey keeps the page out
of them. Trace, screenshot and video are off, and that is not enough: Playwright
also writes an ARIA snapshot of the page into `error-context.md` on any failure. The
spec suppresses both sources of that snapshot and reports each failed step by name
only; the comment at the top of the spec says how. Any new journey that renders
learner data needs the same treatment.

## Helpers

| File | Purpose |
|---|---|
| `helpers/sign-in.ts` | Starts at `/staff-dashboard/` and hands off to `../shared/olapps-sign-in.ts` |
| `helpers/signed-in-test.ts` | `test` whose `page` fixture is already signed in |

## Sign-in flow

Measured against RC with `curl` and Playwright:

- `/staff-dashboard/` is redirected server-side by mitxonline's
  `staff_dashboard_signin_redirect_to_site_signin` view to `/login/`, which APISIX
  sends to `sso-qa.ol.mit.edu`, realm `olapps`, client `ol-mitlearn-client`, with
  `redirect_uri` back to `https://rc.mitxonline.mit.edu/login/`. This passes whether
  or not the dashboard's JavaScript loads.
- Any deeper route, e.g. `/staff-dashboard/flexible_pricing`, is served the app
  shell. The app requests `/api/v0/users/me`, finds no staff session and sends the
  browser to `/login`. This is the path that exercises the bundle: with its scripts
  blocked, the profile request never happens and the journey fails.
- The MITx Online homepage redirects to MIT Learn, so sign-in starts at the
  dashboard rather than the homepage. MIT Learn's session is not MITx Online's.

From Keycloak on, the flow is the realm's, shared with mit-learn: see the login flow
and lockout notes in `../mit-learn/README.md`.

## Content dependencies

None. The signed-in journey asserts that the Flexible Pricing table renders, not
what is in it; an empty list is a legitimate state.
