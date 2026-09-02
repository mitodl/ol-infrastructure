# 0011. Playwright Canary Specs Live in ol-infrastructure, on the Stock Playwright Image

**Status:** Accepted
**Date:** 2026-09-01
**Deciders:** cpatti, Infrastructure team
**Technical Story:** `wp-playwright-canary-meta-pipeline-for-ol-web-prope-47a987` /
`tk-decision-where-playwright-specs-live-and-how-the-108357`

## Context

### Current Situation

We are building a Concourse meta pipeline that manages a fleet of end-to-end,
browser-driven user journey tests — "canaries" in the AWS CloudWatch Synthetics sense
— one managed pipeline per target web property, modeled on
`src/ol_concourse/pipelines/infrastructure/simple_pulumi/`. The first managed canary is
MIT Learn against <https://rc.learn.mit.edu>, with a login-then-course-search journey.

Grafana Synthetic Monitoring
(`src/ol_infrastructure/infrastructure/grafana_alerting/metric_rules/synthetic_monitoring.py`)
already runs plain HTTP availability probes against these properties. This project is
complementary: real browser journeys through login and search, not availability pings.

### Problem Statement

Where do the Playwright specs live, and how are they packaged so a Concourse job can
run them? Nearly every other task in the project depends on the answer: it determines
the shape of `CanaryParams`, the meta pipeline's watched paths, the credential flow,
and who owns a failing canary.

### Constraints

- `ol-infrastructure` is a **Python monorepo with, before this change, zero
  JavaScript**: no `package.json`, no lockfile, no Node tooling in
  `.pre-commit-config.yaml`, and 0 `.ts`/`.tsx` files.
- The ECR pull-through cache covers only `public.ecr.aws` (`ecr-public`) and Docker Hub
  (`dockerhub`) — see `src/ol_infrastructure/infrastructure/aws/ecr/__main__.py`. There
  is **no cache rule for `mcr.microsoft.com`**, where the official Playwright images
  are published, and MCR is not an upstream that ECR pull-through supports. Pulling MCR
  directly is nonetheless consistent with existing practice: pipelines already pull
  `quay.io/keycloak/keycloak` and `ghcr.io/astral-sh/uv` directly.
- Canaries run against live deployed environments, including production, and need real
  login credentials at run time.

### Findings That Drove The Decision

Established empirically on 2026-09-01, not assumed:

1. **`mitodl/mit-learn` already has a Playwright suite.** `playwright.config.ts` plus
   `e2e/smoke.spec.ts`, parameterized by `PLAYWRIGHT_BASE_URL`, with a `playwright:rc`
   script already pointed at `https://rc.learn.mit.edu` and a `login()` helper that
   already handles the multi-screen RC/production Keycloak flow.
2. **That suite is not reusable as a canary.** It is a root-level member of a Yarn 4 /
   Node 24 workspace (`workspaces: ["frontends"]`), so running it means installing the
   whole application monorepo. Its assertions are pinned to CMS-authored copy, specific
   course and program IDs, and certificate prices (`"Certificate Track: $1,000.00"`).
   It also keys expected data off the base URL and **silently falls back to the local
   fixture branch for any unrecognised URL**, so pointing it at a new environment
   quietly asserts the wrong data.
3. **The stock Playwright image does not let you skip an install.** Browsers are baked
   at `/ms-playwright` (`PLAYWRIGHT_BROWSERS_PATH` is preset), but `@playwright/test`
   is *not* installed globally — `/usr/lib/node_modules` holds only `corepack`, `npm`
   and `yarn`. `npx --yes playwright test` therefore fails to resolve
   `@playwright/test` from the config. An install step is mandatory.
4. **That install is nearly free when the dependency set is tiny.** With
   `@playwright/test`, `@types/node`, and TypeScript as the only direct dependencies,
   `npm ci` installs **6 packages in ~400 ms**, and no browser download happens because
   the image already has them. A full canary run against RC completed in ~1.1 s. This
   is what makes a purpose-built
   container image unnecessary machinery rather than an optimization.
5. **The image is acceptable to pull:** `mcr.microsoft.com/playwright:v1.62.1-noble`
   carries ~2.4 GB of filesystem content, 1.3 GB of which is the baked browsers under
   `/ms-playwright`; `docker images` accounts it as 3.53 GB. It pulled in ~20 s on a
   developer connection and is multi-arch with `linux/amd64`. Note that
   `docker image inspect --format '{{.Size}}'` reports 0.95 GB for this image, which
   does not match the filesystem — do not quote it.
6. **Runner and image versions cannot drift.** Pinning `@playwright/test` 1.58.1 against
   the `v1.62.1-noble` image fails with
   `browserType.launch: Executable doesn't exist at /ms-playwright/chromium_headless_shell-1208/...`
   — a message naming neither the pin nor the image tag. Any approach that keeps the pin
   and the image tag in separate repositories has to guard this; one that keeps them
   together can make it impossible instead.

### Options Considered

1. **Specs in `ol-infrastructure` under the canary pipeline directory** — *chosen*
   - Pros: the `@playwright/test` pin and the pipeline that pulls the matching image
     live in one repository, so the version is a single source of truth and finding 6
     stops being a hazard. One git resource, not two. Adding a journey is one PR. No
     third repository for a debugger to discover.
   - Cons: introduces the first JavaScript/TypeScript, `package.json` and lockfile into
     a Python monorepo, and with it a Node toolchain that a Python-and-Pulumi reviewer
     pool now has to care about.

2. **Specs in the target application's own repo** (e.g. `mitodl/mit-learn`)
   - Pros: tests live with the app; app developers own them; for MIT Learn the specs and
     a working RC login helper already exist.
   - Cons: canary fleet health depends on N repos; the meta pipeline needs a git resource
     and watched paths per property. Concretely, finding 2: running the existing suite
     means a full Yarn workspace install, and its assertions are tied to CMS copy and
     prices — an editor changing a word pages an on-call engineer. PR tests and canaries
     have opposite failure economics (strict vs. stable) and pull the same file in
     opposite directions.

3. **A purpose-built canary image built and pushed to ECR by this pipeline**
   - Pros: most reproducible, fastest at run time.
   - Cons: most machinery — a `BuildConfig`/`DockerImageConfig` path, an ECR repo, and an
     image-build job to maintain. Finding 4 removes the motivation: the run-time cost it
     would optimize away is ~400 ms.

4. **A dedicated canary repository** (`mitodl/end-user-test-canaries`)
   - Pros: keeps Node tooling out of the Python monorepo; one repo owns the canary fleet
     regardless of how many properties it covers.
   - Cons: makes the `@playwright/test` pin and the pipeline's image tag a **cross-repo
     lockstep pair**, so finding 6 must be guarded by a check script and a Renovate PR
     bumping the pin is not independently mergeable. Adds a third repository to the
     debugging path, and a repository-registration obligation against the
     Pulumi-managed repo fleet.

## Decision

**Chosen Option:** Option 1 — specs in `ol-infrastructure` under
`src/ol_concourse/pipelines/canaries/`, run against the **stock**
`mcr.microsoft.com/playwright` image with a pinned `npm ci`. No image is built.

**This decision was reversed once during the same session, and the reversal is the
substance of it.** Option 4 was chosen first and a repository
(`mitodl/end-user-test-canaries`) was created. Re-examining it against the codebase
showed two of the arguments for separation did not survive contact with the facts:

- *"Don't grant canary spec authors commits in the repository that deploys
  production."* Wrong. `data/repos/ol-infrastructure.yaml` already grants
  `odl-engineering: maintain`, so every engineer already has more access than that.
- *"Spec edits become commits in the repository that deploys production."* Overstated.
  Concourse path-watching already scopes this precisely; that is exactly what
  `PULUMI_WATCHED_PATHS` and `project_secrets_paths` exist to do. Specs under the canary
  directory trigger canary pipelines and nothing else.

With those removed, the remaining case for a separate repository was tidiness, and its
cost was concrete: a cross-repo version lockstep requiring a guard script, plus a
`pulumi import` of the already-created repository before the 316-repo fleet stack could
apply. That is more machinery than the tidiness was worth.

**Rationale:**

- Findings 3, 4 and 6 together are the crux. An install step is unavoidable, and it is
  only cheap while the dependency set stays tiny — true here, false in an application
  workspace. And because the pin and the pipeline are now in one repository, the
  pipeline **derives** the image tag from `package.json` (`1.62.1` →
  `mcr.microsoft.com/playwright:v1.62.1-noble`) instead of carrying its own copy.
  Finding 6's failure mode becomes unreachable by construction rather than guarded, and
  a Renovate bump of the pin is self-contained.
- The distinction that matters is not *where the code lives* but *what the test is for*.
  A PR gate must be strict; a canary must be stable, because it wakes a human. Finding 2
  shows those requirements actively fighting inside one file. Keeping canaries as their
  own specs — in whichever repo — is what lets each be correct.
- The cost actually paid is one `package.json` in one directory. `.pre-commit-config.yaml`
  has no Node hooks, `ruff` and `mypy` ignore the directory, and nothing else in the
  repository needs to know it exists.

**Key Implementation Details:**

- `package.json` is the single source of truth for the Playwright version.
  `CanaryParams` must **derive** the image tag from it and never hardcode one.
- `playwright.config.ts` **throws** when `CANARY_BASE_URL` is unset. A canary with a
  default target reports green against the wrong environment, which is worse than one
  that refuses to start.
- Credentials are never committed. They arrive as environment variables sourced from
  Vault. `mit-learn`'s `e2e/smoke.spec.ts` currently hardcodes a live RC login in public
  source — the failure mode this rule exists to prevent.
- Chromium runs with `--disable-dev-shm-usage`. A Concourse task container gets the 64 MB
  default `/dev/shm`, which is where Chromium places renderer shared memory; without the
  flag a journey dies mid-run as a bare tab crash.
- `forbidOnly` is unconditional, unlike an application suite that only sets it under CI.
  A stray `.only` in a canary silently stops every other journey for that property from
  being checked, and nothing would report the gap.
- Dependency discipline is a written rule in the directory's `AGENTS.md`: only
  `@playwright/test`, `@types/node`, and TypeScript. Finding 4's economics hold only
  while that is true.

## Consequences

### Positive Consequences

- The Playwright version is one string in one file. No cross-repo lockstep, no guard
  script, no "this Renovate PR is not independently mergeable" caveat.
- One git resource in the canary pipelines, and `CanaryParams` needs no
  `spec_repo`/`spec_ref` fields.
- Onboarding a property stays two list edits, matching `simple_pulumi`; adding a
  *journey* to an existing property needs no pipeline change at all.
- Nothing to register against the Pulumi-managed repository fleet, and no
  `pulumi import` obligation.
- A canary failure is debugged in one repository, by people who already have access.

### Negative Consequences

- `ol-infrastructure` now contains JavaScript. The `AGENTS.md` in that directory exists
  to keep the blast radius at one directory, but the precedent is real and the next
  person wanting a `package.json` elsewhere will cite it.
- Renovate will now raise Node dependency PRs against a repository whose reviewers are
  Python and Pulumi engineers.
- Journeys for MIT Learn must be written fresh rather than reusing the existing `e2e/`
  suite. The `login()` helper's knowledge of the multi-screen Keycloak flow is worth
  porting; its data fixtures are not.
- Duplication of intent with `mit-learn`'s `e2e/` suite is now possible — two places
  test login. Accepted deliberately: they test different things (does this change break
  login vs. is login broken for users right now).

### Neutral Consequences

- `mitodl/end-user-test-canaries` was created before the reversal and **deleted the
  same day**, once its only commit (the scaffold now living under
  `src/ol_concourse/pipelines/canaries/`) had been superseded. It was never registered
  in the Pulumi repository fleet, so nothing there needs unwinding.
- Alerting for these canaries must stay distinct from Grafana Synthetic Monitoring's
  HTTP probes so the two do not double-page. Tracked separately.

## Implementation Notes

- **Risk Level:** Low. Nothing here changes existing infrastructure; the canary fleet is
  additive and read-only with respect to the properties it exercises.
- **Dependencies:** Canary login credentials sourced via SOPS/Vault
  (`tk-source-the-mit-learn-rc-canary-login-credentials-1125c7`) block the first real
  journey, not this decision.
- **Verification performed:** `npm ci`, `npm run typecheck`, image-tag derivation from
  `package.json`, and a passing journey against `https://rc.learn.mit.edu`, all inside
  `mcr.microsoft.com/playwright:v1.62.1-noble`.

## Related Decisions

- `src/ol_concourse/pipelines/infrastructure/simple_pulumi/` — the meta pipeline and
  params-registry pattern this fleet copies.
- `src/ol_infrastructure/infrastructure/grafana_alerting/metric_rules/synthetic_monitoring.py`
  — the HTTP-probe monitoring this complements rather than replaces.
- `src/ol_infrastructure/infrastructure/aws/ecr/__main__.py` — the pull-through cache
  rules, and why MCR is pulled directly.

## References

- Playwright Docker images: <https://playwright.dev/docs/docker>
- `mitodl/mit-learn` `e2e/README.md` — states the intent to run that suite against RC as
  an automated quality gate, the adjacent goal this decision separates from.

---

**Review History:**

| Date | Reviewer | Decision | Notes |
|------|----------|----------|-------|
| 2026-09-01 | cpatti | Approved | Initially chose a dedicated repo; reversed within the session to specs-in-ol-infrastructure once the separation arguments were checked against the codebase |

**Last Updated:** 2026-09-01
