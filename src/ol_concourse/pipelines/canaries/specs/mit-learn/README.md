# mit-learn canaries

**Property:** MIT Learn — <https://learn.mit.edu>
**First target environment:** RC — <https://rc.learn.mit.edu>
**Owner:** MIT Open Learning infrastructure / devops

## Journeys

| Spec | Journey | Status |
|---|---|---|
| `homepage.spec.ts` | Homepage renders in a real browser, anonymously | Active |
| `login-and-search.spec.ts` | Log in, reach the dashboard, then search for courses | Active |

## Helpers

| File | Purpose |
|---|---|
| `helpers/sign-in.ts` | Drives the real multi-screen Keycloak login from the homepage |
| `helpers/signed-in-test.ts` | `test` whose `page` fixture is already signed in |

A journey that needs a session imports `test` from `helpers/signed-in-test` and takes
the ordinary `{ page }` fixture — do not call `signIn` yourself. The session is
established once per worker and reused, so adding a third signed-in journey costs no
extra logins. Journeys that must be anonymous, like `homepage.spec.ts`, keep importing
`@playwright/test` directly.

The session is held in memory and deliberately never written to disk: a `storageState`
file carries live tokens and this project's failure artifacts are published. For the
same reason the login context is not traced, so `sign-in.ts` puts the diagnosis in its
error messages instead.

## Login flow

RC authenticates against `sso-qa.ol.mit.edu`, realm `olapps`, client `ol-mitlearn-client`.
The flow is **identity-first** and measured as three screens:

| Screen | Path | Form control |
|---|---|---|
| Email | `/protocol/openid-connect/auth` | label `Email`, button `Next` |
| Password (account exists in the realm) | `/login-actions/authenticate` | label `Password`, button `Next` |
| Signup (unknown non-MIT address) | `/login-actions/registration` | **has a captcha** |
| Touchstone hand-off (unknown `@mit.edu` address) | `/broker/touchstone-idp/login` → `okta.mit.edu` | MIT credentials |

There is **no captcha on the login path**, so the canary drives the real UI; no bypass,
dedicated flow or injected `storageState` is needed.

Credentials come from the environment (`CANARY_USER_EMAIL`, `CANARY_USER_PASSWORD`),
sourced from Vault by the pipeline. Never commit them — see `../../AGENTS.md`.

### Two things a login journey here must do

Both are implemented in `helpers/sign-in.ts`; they are recorded here because they are
properties of the realm, not of the code, and the next property to authenticate against
`olapps` will need them too.

1. **Assert the password screen was reached, positively.** The flow is identity-first, so
   an account that is missing, disabled or renamed never produces a login error —
   Keycloak just sends the browser elsewhere, and *which* elsewhere depends on the email
   domain. The canary account is `@mit.edu`, so its failure mode is a silent hand-off to
   Touchstone; a non-MIT address instead lands on the captcha'd signup form. Testing for
   those destinations one at a time is how you end up reporting "login now requires SSO"
   or "a captcha now blocks login" when the truth is that the account is gone. Worse, a
   flow that assumes it is on the password screen will type the canary's password into
   whatever page is actually showing — including MIT's own IdP.
2. **Never retry a *rejected* password.** See the lockout note below. Retrying a page that
   failed to load is fine; retrying a refused credential is not. Playwright starts a
   fresh worker process for a retry, so `sign-in.ts` records the refusal in a file under
   `tmpdir` — per-container, so it covers the run and nothing beyond it. Note that the
   realm's custom theme means Keycloak's stock alert markup is absent: the refusal is
   detected by its visible text (`Invalid username or password.`), which is what a user
   sees anyway.

## Canary account

| | |
|---|---|
| Account | `odl-devops+canary-mit-learn-rc@mit.edu` |
| Realm | `olapps` on `sso-qa.ol.mit.edu` (what RC authenticates against) |
| Credential | Vault `secret-concourse/infrastructure/canary_mit_learn`, keys `email` / `password` |
| Source of truth | `src/bridge/secrets/concourse/operations.production.yaml` under `pipelines:`, applied by the `concourse` Pulumi project |
| Referenced as | `CanaryParams.credential_secret` set to `canary_mit_learn` in `../../pipeline.py` |

The address is a plus-address on the devops list deliberately: the domain is one MIT
controls, so a password-reset mail can never be received by anyone else, and anything the
account does generate reaches a monitored mailbox instead of bouncing.

**This user is not Pulumi-managed.** There are no `keycloak.User` resources in this
repository, so the account was created through the admin API and exists only in the
realm. It will not be recreated by a `pulumi up`, and nothing will detect its removal
except the canary failing. It carries `emailVerified=true` and an empty
`requiredActions` — the realm sets `verifyEmail=true` and has `VERIFY_EMAIL` as a
*default action*, so a newly created user gets that action attached and cannot log in
until it is cleared. It also needs the `fullName` attribute, which the realm's user
profile marks required, and which rejects parentheses.

### Rotation must be atomic, or the account is disabled

Realm `olapps` is configured `failureFactor=10`, `permanentLockout=true`,
`maxTemporaryLockouts=1`, `maxDeltaTimeSeconds=43200`. Ten consecutive failed logins
gives one temporary lockout; the next ten **permanently disable the account**, which
needs an admin to undo. The 12-hour reset window means a scheduled canary never lets the
failure counter age out, so failures accumulate until lockout rather than settling into a
harmless recurring error.

So: **change Keycloak and the SOPS/Vault value together.** The gap between the two is
itself enough to disable the account, and it will present as a captcha error rather than
an auth error.

### First-login onboarding, already cleared

The first successful login redirects to `/onboarding?next=…&is_new_user=1`, not the
dashboard. That has already been consumed for this account — subsequent logins land on
`/dashboard`, and `/search?q=…` is reachable directly while authenticated. A *new* canary
account would hit onboarding again, so anyone provisioning one should log in once by hand
before relying on a journey that expects the dashboard.

## Content dependencies

`homepage.spec.ts` has none, by design. Any journey added here that depends on specific
course or program content must record that dependency in this table, because content
that merely happens to exist in RC today is a future false page:

| Journey | Requires | Guaranteed by |
|---|---|---|
| `login-and-search.spec.ts` | A search for `mathematics` returns at least one **course** | Nothing contractual — see below |

That query is deliberately broad: MIT's catalogue not containing a single mathematics
course is not a realistic content change, so the assertion is a real signal about search
rather than a bet on one course's continued existence. It is still a content dependency,
and the honest reading of a failure is "search returned nothing", which is a **failure
worth paging on** — an emptied or half-rebuilt index looks exactly like this from a
user's seat. Anything narrower, such as a named course or an exact result count, is a
false page waiting for the next content sync.

## Prior art

`mitodl/mit-learn` has its own Playwright suite at `e2e/` that can be pointed at RC via
`yarn playwright:rc`. It is a good application test suite and deliberately not reused
here: it is a member of a Yarn 4 workspace, so running it installs the whole
application monorepo, and its assertions are pinned to CMS copy, course IDs and
certificate prices. Its `login()` helper's knowledge of the multi-screen Keycloak flow
is worth porting; its data fixtures are not.
