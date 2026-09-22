# mit-learn canaries

**Property:** MIT Learn — <https://learn.mit.edu>
**First target environment:** RC — <https://rc.learn.mit.edu>
**Owner:** MIT Open Learning infrastructure / devops

## Journeys

Ranked by the production traffic each one stands in for. The ranking is measured,
not assumed — see [Where the journeys come from](#where-the-journeys-come-from).

| Spec | Journey | Route, and its production share | Auth | Status |
|---|---|---|---|---|
| `channel-and-drawer.spec.ts` | Channel page renders its indexed listing; a card opens the resource drawer, and the same link re-enters server-side | `/c/[channelType]/[name]` — **#1**, 39,086 non-bot renders/day | anonymous | Active |
| `search-direct-url.spec.ts` | Search entered as a URL with query and facet parameters | `/search` — **#2**, 21,470/day | anonymous | Active |
| `homepage.spec.ts` | Homepage renders in a real browser | `/` — #3, 6,243/day | anonymous | Active |
| `login-and-search.spec.ts` | Log in, reach the dashboard, then search from the header box | `/dashboard` — #11 by volume at 121/day, but **#1 by cost of silent failure** | signed in | Active |

The two search journeys are deliberately separate rather than merged. They fail for
different reasons: `login-and-search.spec.ts` covers typing into the homepage header
box, which breaks when that affordance breaks, and `search-direct-url.spec.ts` covers
the entry that actually dominates production — a URL carrying `q` and facet
parameters — which breaks when the server cannot turn parameters into results.

`/dashboard` earns its place on blast radius, not volume: 121 renders a day, and every
authenticated journey is behind it.

### Deferred, with reasons

The journey catalogue ranked two more that are **not** implemented. Both are blocked on
provisioning learner state on RC, not on spec-writing, and both were checked rather than
assumed:

| Journey | Route | Why it is not here |
|---|---|---|
| Certificate view | `/certificate/[certificateType]/[uuid]` | Needs a certificate UUID that exists on RC by contract. `/api/v0/program_certificates/` returns `[]` for the canary account — it holds none — and a route with an unknown UUID answers **404**. No endpoint enumerates certificate UUIDs, so the id could not be derived at runtime the way the drawer's is; it would have to be pinned, and a pinned credential-bearing fixture is precisely the content dependency this property avoids. Issuing one means completing a course as the canary, which writes learner state. |
| B2B contract dashboard | `/dashboard/organization/[orgSlug]/contract/[contractSlug]` | Needs the canary account to be a member of a B2B organization holding a contract. Measured: the canary's dashboard renders no `/dashboard/organization/...` link, and RC's mit-learn v0 API exposes no organization, contract or b2b path at all — the data comes from MITx Online. Requires an org, a contract and a membership to be provisioned first. |

Both are worth having — the certificate view is the highest human-signal route on the
whole property at 0.2% bot traffic, and the B2B dashboard is contractual and spans two
services. They are follow-ups, not omissions.

## Where the journeys come from

The journeys above are not a judgement call about what users probably do. They were
derived from production OTEL traces: `resource.service.name="learn-nextjs"` in Tempo
carries `span.next.route`, the Next.js route pattern per page render, which is the same
layer a Playwright journey replays. Ranked over two independent 24h windows on different
weekdays (Tempo caps TraceQL metrics at 25h, so one long window is not available); the
top six routes come out in the same order in both, within ~±30% on volume. Bot share was
measured per route from the raw user-agent header, counting only self-identifying
crawlers, so the non-bot figures above are a floor.

Two findings from that work shape the specs and are not obvious from the source:

- **Search is served by `vector_learning_resources_search` with `hybrid_search=true`**,
  not by `learning_resources_search`. A green search journey is a statement that the
  hybrid/vector path is up.
- **`?resource=<id>&resource_title=<slug>` is a shared resource-drawer deep link** that
  appears on both the #1 and #2 routes, which is why one drawer assertion is worth more
  than its line count.

Before adding a journey here, check it against that ranking rather than against
intuition. The full catalogue, including the per-page API fan-out measured from
individual production traces, is on the task
`tk-mine-production-otel-traces-to-derive-the-mit-le-0dd5fe`.

## Run duration

Measured in the pipeline's own image against RC, Chromium, all four specs:

| Run | Tests | Wall clock |
|---|---|---|
| 1 | 6 | 9.9s |
| 2 | 6 | 14.3s |

The spread is not the test count. It is one test: the header-box search in
`login-and-search.spec.ts` took 1.8s in the first run and 6.3s in the second, because
its `toPass` wrapper retries the keystroke until hydration has attached a handler. The
other five are steady at 0.6–1.6s each.

This is worth stating because the obvious reading of a ~14s run — that the suite is
growing into its 15s `expect_timeout` and the timeout needs raising — is wrong, and
acting on it would mask the real behaviour. Three journeys were added here and the total
did not move; per-test time is what the 90s `timeout` and 15s `expect_timeout` bound,
and no test is close to either. Revisit those numbers when a *single* test gets slow,
not when the suite gets longer.

## Helpers

| File | Purpose |
|---|---|
| `helpers/sign-in.ts` | Drives the real multi-screen Keycloak login from the homepage |
| `helpers/signed-in-test.ts` | `test` whose `page` fixture is already signed in |
| `helpers/fixtures.ts` | Every piece of live content the journeys depend on, with the argument for each |

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

`homepage.spec.ts` has none, by design. Everything the other journeys depend on lives in
`helpers/fixtures.ts`, so there is one file to re-check rather than one per spec, and
this table mirrors it. Content that merely happens to exist in RC today is a future
false page, so a new dependency goes in both places or neither:

| Journey | Requires | Guaranteed by |
|---|---|---|
| `login-and-search.spec.ts`, `search-direct-url.spec.ts` | A search for `mathematics` returns at least one **course** | Nothing contractual — see below |
| `channel-and-drawer.spec.ts` | The channel `/c/unit/ocw` exists and its index is non-empty | A unit channel is an offeror, so it exists as long as OCW is published at all |
| `channel-and-drawer.spec.ts` | **No specific resource.** The resource under test is read off whichever card the index returns first | — |

That query is deliberately broad: MIT's catalogue not containing a single mathematics
course is not a realistic content change, so the assertion is a real signal about search
rather than a bet on one course's continued existence. It is still a content dependency,
and the honest reading of a failure is "search returned nothing", which is a **failure
worth paging on** — an emptied or half-rebuilt index looks exactly like this from a
user's seat. Anything narrower, such as a named course or an exact result count, is a
false page waiting for the next content sync.

### Why the drawer journey pins no resource

The drawer needs a resource id, and the obvious move is to pick one and hardcode it. Do
not. `channel-and-drawer.spec.ts` clicks whichever card the index returned first and
reads the resource's name off that card, so it has nothing of its own to go stale. A
pinned id that is later unpublished or reindexed produces a red build about the
catalogue rather than about the property, and the production traces show a large share
of `/c/...` and `/search` renders already carrying exactly that error from stale shared
links.

Measured, because the failure is quieter than it sounds: `/c/unit/ocw?resource=99999999`
returns **HTTP 200** with the title `Not Found | MIT Learn`, no drawer, and the
channel's own content still on screen. The SSR fetch happens inside `generateMetadata`,
so a missing resource takes out the page metadata while leaving the body looking healthy.

### Why the drawer journey navigates twice

It is not a repeat. Clicking a card is a **client-side** route change with the app
already loaded; the same URL arriving as a **cold** request makes the server fetch the
resource before anything renders. The second is the measured production path — drawer
links are what get shared and indexed — and it is the one with the `generateMetadata`
failure mode above. The two assertions after the cold load are split on purpose: the
page title is server-rendered and the drawer is not, so together they say whether a
failure was the server fetch or the client render. That is the first question triage
asks.

### Why a unit channel and not the topic channel from the traces

The production trace that the drawer fan-out was read from was a topic channel,
`/c/topic/cybersecurity`. The spec uses `/c/unit/ocw` instead, because topic channels
are curated and measurably thin on RC: cybersecurity indexes **4** resources against
OCW's **42,342**. Both exercise the identical route pattern and the identical API
fan-out, so nothing about the journey is lost — but a journey pinned to the topic
channel is one retagging away from a red build that says nothing about whether MIT Learn
is up.

## Prior art

`mitodl/mit-learn` has its own Playwright suite at `e2e/` that can be pointed at RC via
`yarn playwright:rc`. It is a good application test suite and deliberately not reused
here: it is a member of a Yarn 4 workspace, so running it installs the whole
application monorepo, and its assertions are pinned to CMS copy, course IDs and
certificate prices. Its `login()` helper's knowledge of the multi-screen Keycloak flow
is worth porting; its data fixtures are not.
