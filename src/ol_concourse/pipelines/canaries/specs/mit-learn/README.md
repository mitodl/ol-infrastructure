# mit-learn canaries

**Property:** MIT Learn — <https://learn.mit.edu>
**First target environment:** RC — <https://rc.learn.mit.edu>
**Owner:** MIT Open Learning infrastructure / devops

## Journeys

| Spec | Journey | Status |
|---|---|---|
| `homepage.spec.ts` | Homepage renders in a real browser | Active |
| _(pending)_ | Log in, then search for a course | Not yet written |

## Login flow

RC authenticates against `sso-qa.ol.mit.edu`, realm `olapps`, client `ol-mitlearn-client`.
The flow is **identity-first** and measured as three screens:

| Screen | Path | Form control |
|---|---|---|
| Email | `/protocol/openid-connect/auth` | `input[name="username"]`, `button[name="login"]` |
| Password (existing account) | `/login-actions/authenticate` | `input[name="password"]`, `button[name="login"]` |
| Signup (unknown account) | `/login-actions/registration` | **has a captcha** |

Credentials come from the environment (`CANARY_USER_EMAIL`, `CANARY_USER_PASSWORD`),
sourced from Vault by the pipeline. Never commit them — see `../../AGENTS.md`.

### Two things a login journey here must do

1. **Assert the flow reached `/login-actions/authenticate`, not `/login-actions/registration`.**
   Because the flow is identity-first, an account that is missing, disabled or locked out
   does not produce a login error — Keycloak silently routes to the signup form, which
   *does* have a captcha. Without this assertion the symptom of "our stored password
   drifted" reads as "a captcha now blocks login", which sends triage the wrong way.
2. **Never retry a *rejected* password.** See the lockout note below. Retrying a page that
   failed to load is fine; retrying a refused credential is not.

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
| | | |

## Prior art

`mitodl/mit-learn` has its own Playwright suite at `e2e/` that can be pointed at RC via
`yarn playwright:rc`. It is a good application test suite and deliberately not reused
here: it is a member of a Yarn 4 workspace, so running it installs the whole
application monorepo, and its assertions are pinned to CMS copy, course IDs and
certificate prices. Its `login()` helper's knowledge of the multi-screen Keycloak flow
is worth porting; its data fixtures are not.
