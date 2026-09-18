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
| `staff-dashboard-signed-in.spec.ts` | A staff user reaches the dashboard and the Flexible Pricing list | Written, not scheduled |

The signed-in journey is excluded by `spec_paths` in `../../pipeline.py`, and the
property has no `credential_secret`, because it needs an RC account with `is_staff`
on MITx Online and that is a privilege grant nobody has approved yet. The choices are
granting `is_staff` to the existing mit-learn canary account (it already signs in to
RC MITx Online, through the same `olapps` realm and `ol-mitlearn-client` client) or
provisioning a dedicated account. Either way the account must be staff and not
superuser. Sharing the mit-learn account has a cost: each pipeline keeps its own
rejected-credential marker, so a drifted password is submitted by both, twice the
failures toward the realm's permanent lockout (see `../mit-learn/README.md`), and a
rotation has to update both credential secrets at once. To schedule it, drop
`spec_paths` and set `credential_secret`.

While `spec_paths` is narrowed, a new journey added to this directory does **not**
run until it is added to that list too.

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
