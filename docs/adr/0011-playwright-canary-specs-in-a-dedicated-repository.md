# 0011. Playwright Canary Specs Live in a Dedicated Repository

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

- `ol-infrastructure` is a **Python monorepo with zero JavaScript**: no `package.json`,
  no lockfile, no Node tooling in `.pre-commit-config.yaml`, and 0 `.ts`/`.tsx` files.
- The ECR pull-through cache covers only `public.ecr.aws` (`ecr-public`) and Docker Hub
  (`dockerhub`) — see `src/ol_infrastructure/infrastructure/aws/ecr/__main__.py`. There
  is **no cache rule for `mcr.microsoft.com`**, where the official Playwright images
  are published, and MCR is not an upstream ECR pull-through supports. Pulling MCR
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
4. **That install is nearly free when the repo is dedicated.** With `@playwright/test`
   plus TypeScript as the only dependencies, `npm ci` is **6 packages in 335 ms**, and
   no browser download happens because the image already has them. A full canary run
   against RC completed in ~1.2 s. This is what makes a purpose-built container image
   (option c) unnecessary machinery rather than an optimization.
5. **The image is small and fast to pull:** `mcr.microsoft.com/playwright:v1.62.1-noble`
   is 0.95 GB on disk, pulled in ~20 s, multi-arch with `linux/amd64`.
6. **Runner and image versions cannot drift.** Pinning `@playwright/test` 1.58.1 against
   the `v1.62.1-noble` image fails with
   `browserType.launch: Executable doesn't exist at /ms-playwright/chromium_headless_shell-1208/...`
   — a message naming neither the pin nor the image tag. This is the primary operational
   footgun of any stock-image approach and must be guarded, not documented.

### Options Considered

1. **Specs in `ol-infrastructure` under the canary pipeline directory**
   - Pros: cheapest to start; no image build; specs sit next to the pipeline that runs
     them; single repo to change when onboarding a canary.
   - Cons: introduces the first JavaScript/TypeScript, `package.json`, lockfile and Node
     toolchain into a 467-file Python monorepo, along with its own Renovate stream and
     lint/format story. Couples an ~91K-line infrastructure repo's watched paths to test
     authoring, so every spec edit is a commit in the repo that also deploys production.

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
     would optimize away is 335 ms.

4. **A dedicated canary repository** (`mitodl/end-user-test-canaries`) — *chosen*
   - Pros: keeps Node tooling out of the Python monorepo and application test data out
     of the canaries; one repo owns the whole canary fleet regardless of how many
     properties it covers; a canary spec change is not a commit in the repo that deploys
     production; the meta pipeline watches exactly one extra git resource, not N.
   - Cons: a third repo in the loop; contributors must know it exists; it is one more
     Renovate stream that must be kept in lockstep with the pipeline's image tag.

## Decision

**Chosen Option:** Option 4 — a dedicated repository,
[`mitodl/end-user-test-canaries`](https://github.com/mitodl/end-user-test-canaries),
public, running against the **stock** `mcr.microsoft.com/playwright` image with a pinned
`npm ci`.

This is a refinement of option (a)'s packaging (stock image, checked-out specs, no image
build) placed in its own repository rather than in `ol-infrastructure`. It takes option
(b)'s "tests are their own artifact with their own owner" property without taking its
"fleet health depends on N application repos" cost, and it declines option (c) on the
strength of finding 4.

**Rationale:**

- Findings 3 and 4 together are the crux. An install step is unavoidable, but it is only
  cheap while the dependency set stays tiny — which is true in a dedicated repo and
  false in an application workspace. The same fact that kills the naive "no install
  needed" version of option (a) also kills option (c)'s justification.
- The distinction that matters is not *where the code lives* but *what the test is for*.
  A PR gate must be strict; a canary must be stable, because it wakes a human. Finding 2
  shows those requirements actively fighting inside one file. Separate artifacts let
  each be correct.
- `ol-infrastructure` having exactly zero JavaScript is a property worth keeping. Adding
  a Node toolchain to it for canary specs is a permanent tax on a repo whose reviewers
  are Python and Pulumi engineers.

**Key Implementation Details:**

- The canary repo pins `@playwright/test` and the pipeline pulls the matching
  `mcr.microsoft.com/playwright:v<version>-noble` tag. `CanaryParams` carries
  `playwright_version` as the pipeline-side half of that pair.
- `bin/check-image-pin.mjs` in the canary repo fails fast when the two disagree,
  converting finding 6's cryptic `Executable doesn't exist` into a message naming both
  versions. The pipeline runs it before any journey.
- `playwright.config.ts` **throws** when `CANARY_BASE_URL` is unset. A canary with a
  default target reports green against the wrong environment, which is worse than one
  that refuses to start.
- Credentials are never committed. They arrive as environment variables sourced from
  Vault. The repository is public, and `mit-learn`'s `e2e/smoke.spec.ts` currently
  hardcodes a live RC login — the failure mode this rule exists to prevent.
- Chromium runs with `--disable-dev-shm-usage`. A Concourse task container gets the 64 MB
  default `/dev/shm`, which is where Chromium places renderer shared memory; without the
  flag a journey dies mid-run as a bare tab crash.
- `forbidOnly` is unconditional, unlike an application suite that only sets it under CI.
  A stray `.only` in a canary silently stops every other journey for that property from
  being checked, and nothing would report the gap.

## Consequences

### Positive Consequences

- `ol-infrastructure` stays Python-only; no Node toolchain, lockfile or JS lint config.
- Onboarding a property stays two list edits in `ol-infrastructure`, matching
  `simple_pulumi`; adding a *journey* to an existing property needs no pipeline change
  at all.
- `npm ci` stays a sub-second, 6-package install, and stays that way only if the repo's
  dependency discipline holds — which is now an explicit rule in its `AGENTS.md`.
- Canary specs are reviewable by the people who own the journeys without granting them
  commits in the repository that deploys production infrastructure.
- Moving later to option (c) remains a `CanaryParams` field addition plus a build job,
  not a rewrite, because the specs are already an independently checked-out artifact.

### Negative Consequences

- A third repository in the loop for anyone debugging a canary failure.
- The `@playwright/test` pin and the pipeline's image tag are a two-repo lockstep pair.
  A Renovate PR bumping the pin alone is **not** independently mergeable. Guarded by
  `check:image-pin`, but it is a real cross-repo coupling.
- Journeys for MIT Learn must be written fresh rather than reusing the existing `e2e/`
  suite. The `login()` helper's knowledge of the multi-screen Keycloak flow is worth
  porting; its data fixtures are not.
- Duplication of intent with `mit-learn`'s `e2e/` suite is now possible — two places
  test login. Accepted deliberately: they are testing different things (does this change
  break login vs. is login broken for users right now).

### Neutral Consequences

- The canary repo is **not** currently registered in the Pulumi-managed repository fleet
  (`src/ol_infrastructure/saas/github/repositories/`). Bringing it under management
  requires importing the already-created `github.Repository` into stack state first: the
  provider's create is `POST /orgs/mitodl/repos`, which returns 422 for an existing repo
  and would hard-fail the whole 316-repo fleet deploy. Registration was deliberately
  deferred rather than half-done. Tracked as follow-up work.
- Alerting for these canaries must stay distinct from Grafana Synthetic Monitoring's
  HTTP probes so the two do not double-page. Tracked separately.

## Implementation Notes

- **Risk Level:** Low. Nothing here changes existing infrastructure; the canary fleet is
  additive and read-only with respect to the properties it exercises.
- **Dependencies:** Canary login credentials sourced via SOPS/Vault
  (`tk-source-the-mit-learn-rc-canary-login-credentials-1125c7`) block the first real
  journey, not this decision.
- **Verification performed:** `npm ci`, `npx tsc --noEmit`, `npm run check:image-pin`,
  and a passing journey against `https://rc.learn.mit.edu`, all inside
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
- `mitodl/mit-learn` `e2e/README.md` — states the intent to run this suite against RC as
  an automated quality gate, which is the adjacent goal this decision separates from.

---

**Review History:**

| Date | Reviewer | Decision | Notes |
|------|----------|----------|-------|
| 2026-09-01 | cpatti | Approved | Chose a dedicated public repo over specs-in-ol-infrastructure; deferred Pulumi fleet registration pending state import |

**Last Updated:** 2026-09-01
