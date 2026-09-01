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

RC and production use a **multi-screen** Keycloak login: email, Next, password, Next.
A local Docker stack presents a single-screen form instead. Canaries only ever run
against deployed environments, so only the multi-screen flow matters here.

Credentials come from the environment (`CANARY_USER_EMAIL`, `CANARY_USER_PASSWORD`),
sourced from Vault by the pipeline. Never commit them — see `../../AGENTS.md`.

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
