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
- **Retry the action, not just the assertion, when interacting before hydration.**
  Auto-waiting gets a locator that is present and enabled, which is not the same as
  one the framework has attached a handler to yet — a keystroke into a
  server-rendered search box is silently dropped. Wrap the action and its outcome in
  `expect(async () => { … }).toPass()` rather than sleeping. `login-and-search.spec.ts`
  does this; the race was caught only because the journey was run in WebKit, where the
  page is slower to hydrate. Chromium passed it every time, which is what this class of
  bug looks like right up until the target has a bad day.

## Failure artifacts

Traces, screenshots and video are retained on failure into `canary-results/`. The
pipeline collects that directory into a task output and, **on failure only**, uploads it
to:

```
s3://ol-eng-artifacts/canary-results/<pipeline>/<job>/<build>/
```

When triaging, the trace is almost always the fastest route — pull down `trace.zip` and
drop it into <https://trace.playwright.dev/>.

Two things about that upload are load-bearing:

- **The collection step runs after a failed test run, on purpose.** The artifacts worth
  having exist only when the run fails, which is exactly when a non-zero exit under
  `set -e` would skip collecting them. `pipeline.py` captures the status and re-raises it
  after the copy. If you restructure that script, keep that ordering or the pipeline
  silently publishes nothing for precisely the runs you need it for.
- **The build is stamped into the directory path, not into the `put`.** A Concourse `put`
  cannot interpolate build metadata, so the run script writes into
  `$BUILD_PIPELINE_NAME/$BUILD_JOB_NAME/$BUILD_NAME` and rclone copies the tree wholesale.
  Uploading to a flat prefix instead would have each failure overwrite the last.

rclone uses `copy`, never `sync` — `sync` mirrors deletions, which against a
build-stamped prefix would erase exactly the history this exists to keep. Credentials
come from the worker instance role (`env_auth = true`); `ol-eng-artifacts` is already in
the operations Concourse IAM policy, so no secret is involved and none should be added.

Green runs collect their report into the output too, but nothing is uploaded. Do not
"fix" that by making the `put` unconditional: at a 10-minute cadence that is a
few-hundred-KB HTML bundle 144 times a day per canary, and it buries the failures.

## Alerting

After every run the task pushes a **gauge** to Grafana Cloud over the Influx
line-protocol endpoint:

```
canary_journey,canary=<name>,target=<host> success=<1|0>
```

Influx names a field `<measurement>_<field>` unless it is literally `value`, so that
arrives in Mimir as `canary_journey_success{canary,target}`. Two rules in
[`metric_rules/canaries.py`](../../../ol_infrastructure/infrastructure/grafana_alerting/metric_rules/canaries.py)
read it: `CanaryJourneyFailing` (the journey broke) and `CanaryNotReporting` (the
canary itself stopped running). Both go to Slack `#devops-warnings`, not to the Rootly
paging path, because the only canary targets an RC environment.

Things to know before touching any of that:

- **A gauge, not a failure counter.** A counter only exists once incremented, so a
  canary that has never failed has no series at all — and `increase()` cannot tell
  that from a canary that stopped running, which is the one failure a canary must never
  hide.
- **Renaming the measurement or the field silently orphans the rules.** The metric just
  stops existing, and an absent series is not an error PromQL can report. Change both
  sides together.
- **Adding a canary needs a third edit.** `CanaryNotReporting` asserts absence against a
  *name* — `absent_over_time` returns no series to group by, so nothing can discover the
  fleet from the data. Add the name to `EXPECTED_CANARIES` in `metric_rules/canaries.py`
  as well as to `pipeline_params` and `canary_names`. A canary missing from that list is
  monitored only while it is already running.
- **Do not make the push fatal.** A Grafana outage must not turn a green canary red;
  `CanaryNotReporting` is the backstop for a push that never lands.
- **Concourse's own build metrics are not a shortcut.** `concourse_builds_latest_completed_build_status`
  looks like exactly the right signal and is per-ATC-node state that never reconciles —
  measured, nodes disagree permanently. See the `metric_rules/canaries.py` docstring.

### Prerequisite: the `grafana` credential must exist for the pipeline's team

Concourse resolves `((grafana.metrics_url))` as `secret-concourse/<team>/grafana`, then
`secret-concourse/shared/grafana`. Today only `main/grafana` exists, and these pipelines
run in the **`infrastructure`** team, so `src/bridge/secrets/concourse/operations.production.yaml`
needs an `infrastructure/grafana` entry with `metrics_url`, `metrics_write_user` and
`metrics_write_token` (the same three values as `main/grafana`).

**An unresolved `((var))` fails the build before the task runs**, so the push cannot be
guarded in bash and must not ship ahead of that entry.

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
