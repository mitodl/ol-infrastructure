# Agent Instructions — `src/ol_concourse/pipelines/canaries`

Playwright canaries for MIT Open Learning web properties. Read [`README.md`](README.md)
first for what a canary is and what does not belong here.

This is the **only JavaScript/TypeScript in `ol-infrastructure`**. Keep it that way:
this directory is a self-contained Playwright project and nothing outside it should
grow a `package.json`.

---

## Non-negotiables

1. **No credentials in source.** This repository is public. Not a test account, not a
   "throwaway" password, not in a fixture, not in a comment. `mitodl/mit-learn`'s
   `e2e/smoke.spec.ts` hardcodes a live RC login; that is exactly the mistake not to
   repeat. Read them from the environment and fail loudly when unset:

   ```ts
   const email = process.env.CANARY_USER_EMAIL
   const password = process.env.CANARY_USER_PASSWORD
   if (!email || !password) {
     throw new Error("CANARY_USER_EMAIL and CANARY_USER_PASSWORD are required")
   }
   ```

   The pipeline sources these from Vault. A canary that falls back to a default
   credential is worse than one that fails to start.

2. **No target URL in source.** `playwright.config.ts` requires `CANARY_BASE_URL` and
   throws without it. Do not add a default, and do not branch on the base URL to pick
   different expected data — see "Do not key assertions on the environment".

3. **Do not add dependencies.** Keep the direct dependency set to
   `@playwright/test`, `@types/node`, and TypeScript. That is what keeps `npm ci` a
   6-package, sub-second install; every dependency added here is paid on every run of
   every canary, forever.

---

## Version pinning

`package.json`'s `@playwright/test` pin is the **single source of truth**. The pipeline
derives the image tag from it — `1.62.1` → `mcr.microsoft.com/playwright:v1.62.1-noble`
— rather than carrying its own copy of the version. Keeping specs and pipeline in one
repository is what makes that possible, and it is most of the reason they are here.

**Never hardcode the image tag in `pipeline.py`.** Derive it from this `package.json`.
If the two are ever allowed to drift, the failure is this, naming neither version:

```
browserType.launch: Executable doesn't exist at
/ms-playwright/chromium_headless_shell-1208/chrome-headless-shell-linux64/...
```

(Reproduced with `@playwright/test` 1.58.1 against `v1.62.1-noble`. The browsers are
baked into the image at a revision keyed to that exact Playwright version.)

A Renovate bump of the pin here is therefore self-contained and safe: it moves the
image tag with it automatically. That is the property to preserve.

Note also that the image does **not** ship `@playwright/test` globally —
`/usr/lib/node_modules` holds only `corepack`, `npm` and `yarn` — so `npm ci` is
mandatory and cannot be optimised away. The browsers are already present, so it
downloads none.

---

## Adding a journey to an existing property

1. Add `specs/<property>/<journey>.spec.ts`.
2. Reuse the property's helpers in `specs/<property>/helpers/`. Login flows in
   particular are shared — do not re-derive a Keycloak flow per spec.
3. Run it locally against the real target (see `README.md`) **at least twice** in a
   row. A canary that passes once is not yet a canary.
4. No pipeline change is needed. A property's pipeline runs every spec under its
   directory.

## Adding a new property

1. `mkdir specs/<property>` with a `README.md` naming the environment it targets and
   who owns the journeys.
2. Add the journeys as above.
3. Add a `CanaryParams` entry in `pipeline.py` and add the name to the list in
   `meta.py` — two list edits, the same onboarding shape as
   [`simple_pulumi`](../infrastructure/simple_pulumi/).

---

## Writing journeys that survive

- **Use role- and label-based locators.** `getByRole("button", { name: "Log In" })`
  over a CSS path. They break when the user-visible affordance breaks, which is the
  signal a canary is for.
- **Do not key assertions on the environment.** `mit-learn`'s app suite keeps a
  `{ [RC_DEFAULT]: {...}, [LOCAL_DEFAULT]: {...} }` table of expected titles and
  prices, and silently falls back to the local branch for any unrecognised base URL —
  so pointing it at a new environment quietly asserts the wrong data. A canary should
  assert something true of every environment.
- **Prefer a stable seeded fixture over live content.** If a journey must reference a
  specific course, it needs a course that exists in every target environment by
  contract. Note that dependency in the property's `README.md`; content that merely
  happens to be there today is a future 3am page.
- **Web-first assertions only.** `expect(locator)` auto-retries; `expect(await
  locator.count())` does not, and is the most common source of canary flake.
- **Never `waitForTimeout`.** Wait for the thing you actually need.

## Failure artifacts

Traces, screenshots and video are retained on failure into `canary-results/`, which the
pipeline publishes. When triaging, the trace is almost always the fastest route — drop
it into <https://trace.playwright.dev/>.

## Validation

Python tooling ignores this directory; `ruff` and `mypy` have nothing to say about it,
and `.pre-commit-config.yaml` has no Node hooks. Validate it directly:

```bash
cd src/ol_concourse/pipelines/canaries
npm ci
npm run typecheck
CANARY_BASE_URL=https://rc.learn.mit.edu npx playwright test specs/<property>
```

## Things that look reasonable and are not

- **Adding a default for `CANARY_BASE_URL`.** A canary silently pointed at the wrong
  environment reports green while the real one burns.
- **Raising `retries` to quiet a flaky journey.** Fix the locator. Retries above 1
  convert real intermittent user-facing breakage into silence.
- **Hoisting `package.json` to the repo root.** It would put a Node toolchain in front
  of every contributor to a Python monorepo. It stays in this directory.
- **`.only` left in a spec.** `forbidOnly` is on unconditionally — not just under CI,
  as an application suite would have it — and will fail the run, on purpose. A stray
  `.only` in a canary silently stops every other journey for that property from being
  checked, and nothing would report the gap.
