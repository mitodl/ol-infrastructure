# Agent Instructions — `src/ol_concourse/pipelines/canaries`

Playwright canaries for MIT Open Learning web properties. Read [`README.md`](README.md)
first for what a canary is and what does not belong here.

Two skills in [`mitodl/agent-kit`](https://github.com/mitodl/agent-kit) carry the
step-by-step procedures, and this file carries the reasoning behind them:

- [**`add-canary-journey`**](https://github.com/mitodl/agent-kit/blob/main/skills/process/add-canary-journey/SKILL.md)
  — adding a journey to a property, or onboarding a new one.
- [**`run-canary-locally`**](https://github.com/mitodl/agent-kit/blob/main/skills/process/run-canary-locally/SKILL.md)
  — getting a passing local run, supplying credentials without locking the account
  out, and reading a trace.

They live in `agent-kit` rather than this repository's `agents/skills/` because that is
where shared agent skills are maintained and distributed from. The cost of that split is
that they can go stale without a failing build here: **if you change a rule in this file,
check whether the matching skill repeats it.**

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
derives the image tag from it — `1.63.0` → `mcr.microsoft.com/playwright:v1.63.0-noble`
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

1. **Check the journey is worth having before writing it.** A property's `README.md`
   carries a ranked journey table derived from production traces. Add against that
   ranking, not against intuition about what users probably do — `specs/mit-learn`'s
   own most-rendered route turned out to be one nobody had thought to cover, while a
   route serving 121 requests a day turned out to be the one worth paging on.
2. Add `specs/<property>/<journey>.spec.ts`.
3. Reuse the property's helpers in `specs/<property>/helpers/`. Login flows in
   particular are shared — do not re-derive a Keycloak flow per spec.
4. **If the journey touches live content, put the reference in
   `specs/<property>/helpers/fixtures.ts` and the property `README.md`'s
   content-dependency table**, not inline in the spec. Two journeys depending on the
   same content should share one constant, so re-checking a dependency is one edit.
   First read "Derive the fixture, do not pin it" below — most content references turn
   out to be avoidable.
5. Run it locally against the real target (see `README.md`) **at least twice** in a
   row. A canary that passes once is not yet a canary.
6. Run it once in WebKit (see "Running WebKit" below). This is not cross-browser
   coverage — the pipeline only schedules Chromium — it is the cheapest way to catch a
   hydration race, because WebKit loses the races that Chromium wins by being faster.
7. No pipeline change is needed. A property's pipeline runs every spec under its
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
- **Derive the fixture, do not pin it.** Before hardcoding an id, a slug or a UUID, ask
  whether the page will hand it to you. `mit-learn`'s drawer journey needs a resource
  id; it clicks whichever card the index returned first and reads the resource's name
  off that card, so it has no resource of its own to go stale. A pinned id that is
  later unpublished or reindexed produces a red build about the *catalogue* rather than
  about the property — and those reds are how a canary trains its audience to ignore
  it. Derivation is not always possible (a certificate UUID cannot be discovered, which
  is why that journey is deferred rather than pinned), but it usually is, and it is
  worth the extra locator.
- **Prefer a stable seeded fixture over live content.** Where a reference is
  unavoidable, it needs something that exists in every target environment by contract,
  and it needs the argument written down. Prefer the structural thing over the curated
  one: a *unit* channel on `mit-learn` is an offeror and exists as long as the offeror
  publishes anything, while a *topic* channel is curated and measurably thin — 4 indexed
  resources against 42,342 for the unit. Both exercise the same route and the same API
  fan-out, so choosing the durable one costs nothing.
- **Scope a listing assertion to the listing.** A page often renders a curated strip
  and an index-fed listing with the same roles, and an unscoped `getByRole("article")`
  matches the curated one first — so it sails straight through an empty search index,
  which is one of the failures a canary exists to catch. On `mit-learn`'s channel page
  the index-fed listing is the `tabpanel`; the "Featured Courses" strip above it is
  configured on the channel. Find the narrower role, and assert the count in the facet
  tab's accessible name rather than the presence of the tab.
- **A cold navigation is a different test from a client-side route change.** Clicking
  through to a URL with the app already loaded exercises the client router; the same URL
  arriving as a fresh request makes the server render it. They have different failure
  modes, and the shared-link path is usually the one that matters in production. On
  `mit-learn` a drawer link with a resource the server cannot fetch answers **HTTP 200**
  with a not-found title, no drawer, and the underlying page still on screen — nothing
  about it looks like an error. Where both paths matter, assert a server-rendered signal
  (the document title) separately from a client-rendered one (the dialog), so a failure
  says which half broke.
- **Web-first assertions only.** `expect(locator)` auto-retries; `expect(await
  locator.count())` does not, and is the most common source of canary flake.
- **Never `waitForTimeout`.** Wait for the thing you actually need.
- **Expect `getByRole` to be ambiguous, and disambiguate on structure.** Strict mode is
  a feature: two matches means the locator does not describe one thing. Reach for
  `level:` on a heading or a narrower ancestor before reaching for `exact: true`, which
  fails more often than it looks like it should — an accessible name is computed, not
  the text you can see. `mit-learn`'s channel `h1` pairs the channel title with a logo
  whose `alt` repeats it, so its accessible name is the title *twice*: a substring match
  also matches the "Search within …" `h2` below it, and an exact match matches nothing
  at all. `{ level: 1, name: title }` is the locator that means what it says.
- **Retry the action, not just the assertion, when interacting before hydration.**
  Auto-waiting gets a locator that is present and enabled, which is not the same as
  one the framework has attached a handler to yet — a keystroke into a
  server-rendered search box is silently dropped. Wrap the action and its outcome in
  `expect(async () => { … }).toPass()` rather than sleeping. `login-and-search.spec.ts`
  does this; the race was caught only because the journey was run in WebKit, where the
  page is slower to hydrate. Chromium passed it every time, which is what this class of
  bug looks like right up until the target has a bad day.

## What triggers a run

Two things, feeding **one job**:

| Trigger | Resource | Set by |
|---|---|---|
| Schedule | `canary-schedule` (`time`) | `CanaryParams.schedule_interval`, default 10m |
| RC deploy | `deploy-marker` (`s3`) | `CanaryParams.deploy_trigger` |

Issue #5592 asked for both — "after a new release to RC and also periodically on a
schedule" — and they answer different questions. The schedule catches breakage no
deploy caused (an expired certificate, an upstream outage, a Keycloak change). The
deploy trigger catches a bad release *now* rather than up to ten minutes later, and
is what lets a run be attributed to the release it tested.

### Why a marker in S3 and not something better

The canary runs in `canary-<property>`; the deploy runs in `<app>-pipeline`. Concourse's
`passed` constraint is **pipeline-local**, and there is no trigger-another-pipeline
primitive, so the only way one pipeline observes the other is a resource both can see.
The alternatives were considered and rejected for concrete reasons, not taste:

- **The app's GitHub Deployment.** The natural fit — it already records "version X is on
  RC" — but `github_deployment()` is a **put-only** resource pinned to
  `check_every: never`, so it emits no versions to trigger on. It is also only created
  by the *release-resource* pipeline shape, which `mit-learn` is not on: as of this
  writing only `ol-analytics-api` sets `AppRegistration.release_resource_workflow`.
  There is no GitHub Deployment for mit-learn to watch.
- **The `mitodl/mit-learn-app` registry image.** Triggers when the image is *built*,
  which is before it is deployed. The canary would test the previous release and
  attribute the result to the new one — worse than not triggering at all.
- **A `fly trigger-job` task in the deploy pipeline.** Needs Concourse credentials in
  the deploy pipeline and produces a build with no input identifying what it tested.

So: the RC deploy job writes
`s3://ol-eng-artifacts/deploy-markers/<app>/RC/<version>.json` and the canary watches
that prefix. The **version is in the object key, not just the body**, because that is
where Concourse's `s3` resource reads a version from — which makes the release calver
the *resource version*, visible on the canary's build page and in its inputs. A marker
carrying the version only in its body would trigger builds that could not be attributed
to anything, which is precisely what the production-gating work downstream needs.

The key layout lives in one place, [`../deploy_markers.py`](../deploy_markers.py), and
both ends build the resource from `deploy_marker_resource()`. **Do not hand-roll either
end.** A producer writing keys the consumer's `regexp` no longer matches fails silently
at both ends: the deploy stays green, and the canary just quietly stops
deploy-triggering while continuing to pass on its schedule. Both meta pipelines watch
that file so a layout change re-renders them together.

### Both halves are required

Onboarding a deploy trigger is two edits in two files:

1. `CanaryParams.deploy_trigger=DeployTrigger(app_name="<app>")` here, and
2. `publish_rc_deploy_marker=True` on that app's `AppPipelineParams` in
   [`../infrastructure/k8s_apps/pipeline.py`](../infrastructure/k8s_apps/pipeline.py).

Setting only the first gives a canary that still runs on schedule and reports `0.0.0.0`
as the release it tested. That is degraded but **not stuck**, which is deliberate: the
marker resource declares an `initial_path`/`initial_version`, so it always has a version
to resolve. Without that, a `get` on a resource that has never had a version never
becomes schedulable — and because both triggers feed one job, that would have stopped
the *scheduled* runs too. If you add a marker-backed trigger anywhere else, keep the
initial version.

### Concurrency

One job with `max_in_flight=1`, not two jobs. A deploy landing mid-run queues a second
run behind the first rather than interleaving with it, so the property never has two
canaries on it at once and no `serial_group` is needed. Two jobs would have needed one,
and would also have split the run history — including the flake record below — across
two places.

The marker is an input to *every* run, scheduled ones included, so each run names the
release it found deployed rather than only deploy-triggered ones doing so.

### Ordering against the Fastly purge

The marker `put` is appended **after** any Fastly purge in the same job. A canary that
woke up before the edge cache was purged would be testing the previous release through
Fastly and reporting it against the new version. If you add another post-deploy step
that has to complete before the property is really serving the new release, put it
before the marker steps.

## Result and failure artifacts

Concourse build status is the sole canary result: a passing journey makes the build
green and a failed journey makes it red. Do not add metric pushes, Grafana alerts,
Slack notifications, or Rootly incidents. This deliberately matches how most of our
pipelines report success and failure.

Two things are published, on deliberately different conditions:

| What | When | Where |
|---|---|---|
| Traces, screenshots, video, HTML report | **failure only** | `s3://ol-eng-artifacts/canary-results/…` |
| `results.json` — Playwright's machine-readable report | **every run** | `s3://ol-eng-artifacts/canary-runs/…` |

The release under test is carried into `results.json` as `config.metadata.releaseRef`,
set from `CANARY_RELEASE_REF` in `playwright.config.ts`. Concourse's build page shows
the same version as the `deploy-marker` resource, but builds are reaped within a handful
of runs and the bucket is not — so the retained record is the only place a flake found
weeks later can still be attributed to a release:

```bash
aws s3 cp s3://ol-eng-artifacts/canary-runs/canary-mit-learn/run-mit-learn-canary/<stamp>.json - \
  | jq '{release: .config.metadata.releaseRef, flaky: .stats.flaky}'
```

It reads `unknown` for a canary with no `deploy_trigger`, and `0.0.0.0` before the app
has ever published a marker. Nothing in a spec should branch on it.

### The failure tree

Traces, screenshots and video are retained on failure into `canary-results/`. The
pipeline collects that directory into a task output and, **on failure only**, uploads it
to:

```
s3://ol-eng-artifacts/canary-results/<pipeline>/<job>/<YYYYMMDDTHHMMSSZ>/
```

When triaging, the trace is almost always the fastest route — pull down `trace.zip` and
drop it into <https://trace.playwright.dev/>.

Two things about that upload are load-bearing:

- **The collection step runs after a failed test run, on purpose.** The artifacts worth
  having exist only when the run fails, which is exactly when a non-zero exit under
  `set -e` would skip collecting them. `pipeline.py` captures the status and re-raises it
  after the copy. If you restructure that script, keep that ordering or the pipeline
  silently publishes nothing for precisely the runs you need it for.
- **The run is stamped into the directory path, not into the `put`.** Uploading to a flat
  prefix would have each failure overwrite the last, and neither end of the job can
  supply a build number: **task containers on our Concourse get `ATC_EXTERNAL_URL` and no
  `BUILD_*` variables**, verified with an `env` probe against cicd.odl.mit.edu, and a
  script expanding `$BUILD_PIPELINE_NAME` therefore dies on `set -u` before any test
  runs. The rclone `put` container does get `BUILD_*`, but a `put` cannot interpolate
  them into its destination. So the pipeline name and job name are rendered in as
  literals (`pipeline.py` knows both) and the task appends a UTC timestamp. Match an
  artifact directory to a build by the build's start time.

rclone uses `copy`, never `sync` — `sync` mirrors deletions, which against a
per-run prefix would erase exactly the history this exists to keep. Credentials
come from the worker instance role (`env_auth = true`); `ol-eng-artifacts` is already in
the operations Concourse IAM policy, so no secret is involved and none should be added.

Green runs collect the tree into the output too, but none of it is uploaded. Do not
"fix" that by making *that* `put` unconditional: at a 10-minute cadence it is a
few-hundred-KB HTML bundle plus traces 144 times a day per canary, and it buries the
failures. What a green run does publish is the small JSON record below.

### The per-run record, and why a flake needs one

`results.json` is uploaded on **every** run, green included, to a separate prefix:

```
s3://ol-eng-artifacts/canary-runs/canary-<property>/<job>/<YYYYMMDDTHHMMSSZ>.json
```

One flat object per run, named for the run, so the whole history is a single
`aws s3 ls` returning one sortable line per run. It shares its timestamp with the
failure tree, so a red build's two records can be matched to each other.

This exists because **a flake is invisible in every other record we keep**. `retries`
is 1, so a journey that fails its first attempt and passes on the second is reported
`flaky` and the run exits **0** — verified directly, not assumed. The build is therefore
green, the failure-only upload publishes nothing, and Playwright's `N flaky` line lives
only in the task log, which Concourse reaps within a handful of builds. The upshot
before this record existed: across 1,360 builds the hard-failure rate was exactly
knowable (99.41% green) and the flake rate was not knowable **at all**.

Build duration is not a usable proxy for it, and that was measured rather than assumed —
`npm ci`, image pull and container setup dominate wall time, so a retry does not move a
build out of the normal 30–60s band.

`results.json` carries `stats.flaky` and the per-attempt status of every test
(`results[].status` is `["failed", "passed"]` for a flake), which is what makes the
question answerable:

```bash
# Runs that retried a journey, over the retained history.
aws s3 ls --recursive s3://ol-eng-artifacts/canary-runs/canary-mit-learn/ \
  | awk '{print $4}' \
  | while read -r key; do
      flaky=$(aws s3 cp "s3://ol-eng-artifacts/$key" - | jq '.stats.flaky')
      [ "$flaky" -gt 0 ] && echo "$key flaky=$flaky"
    done
```

Four things to keep about this step:

- **It is evidence, not a second result signal.** Concourse build status remains the
  only canary result. Do not grow a metric push, Grafana alert, Mimir series or Slack
  notification off the back of this file — that is the standing decision the whole
  design rests on, and a JSON blob in a bucket is not a crack in it.
- **It runs under `ensure`, not as a following step.** A step placed after the task is
  skipped when the task fails, which is exactly the run whose record matters most. On a
  red build `ensure` and `on_failure` both fire, so the same ~6–20KB also lands inside
  the failure tree; that duplication buys a run history that is uniform across green and
  red, and it is worth the few KB.
- **`inputs` on the `put` is load-bearing.** Without it Concourse streams every artifact
  in the plan — the multi-megabyte failure tree and the repository checkout — into the
  put container in order to upload one small file.
- **Neither `put` can break the canary — and the `ensure` one needs a `try` to keep it
  that way.** Concourse propagates a hook failure to its parent: *"If the parent step
  succeeds and the ensured step fails, the overall step fails"*
  ([ensure hook docs](https://concourse-ci.org/ensure-step-hook.html)). So a bare
  `ensure: put` would let a transient rclone/S3/IAM error turn a canary that passed
  every journey **red** — the property fine, the build claiming otherwise. That is the
  precise false positive this whole directory is organised against, so the `ensure` put
  is wrapped in `try`, the same way the Sentry sourcemap upload in
  [`../infrastructure/k8s_apps/pipeline.py`](../infrastructure/k8s_apps/pipeline.py) is.
  Losing one run's evidence is far cheaper than evidence that can contradict the result
  it is evidence about. **Do not unwrap it**, and do not "fix" a missing record by doing
  so. The `on_failure` put is deliberately left bare: it only runs on builds that are
  already red, so it cannot change an outcome, and leaving it unwrapped keeps a broken
  artifact upload visible rather than silent.
- **An empty output is also safe, for a different reason.** When the harness dies before
  any test runs (a bad image, a failed `npm ci`) there is no `results.json` and no tree,
  and both outputs stay empty. Concourse pre-creates a declared output as an empty
  directory, and an rclone copy from an empty directory is a no-op that exits 0 —
  verified against rclone 1.75.1. A *missing* directory would be a different story: the
  resource's `out` script runs `ls` on the source under `set -e`. So keep the `mkdir -p`
  that creates both directories unconditionally, and keep both as declared `outputs`.

## Validation

Python tooling ignores this directory; `ruff` and `mypy` have nothing to say about it,
and `.pre-commit-config.yaml` has no Node hooks. Validate it directly:

```bash
cd src/ol_concourse/pipelines/canaries
npm ci
npm run typecheck
CANARY_BASE_URL=https://rc.learn.mit.edu npx playwright test specs/<property>
```

### Running WebKit

`npx playwright install webkit` downloads the browser but WebKit also needs system
libraries (`libxml2`, `libflite1`) that a workstation generally will not have, and
`playwright install-deps` wants root and assumes apt. Do not install them — run the
same image the pipeline runs instead, which also removes any question of whether a
pass was an artifact of the local machine:

```bash
# Tag comes from package.json, the same derivation pipeline.py makes.
TAG=v$(python3 -c "import json;print(json.load(open('package.json'))['devDependencies']['@playwright/test'])")-noble
rm -rf /tmp/canary-run
mkdir -p /tmp/canary-run
rsync -a --exclude node_modules --exclude canary-results . /tmp/canary-run/
docker run --rm -v /tmp/canary-run:/work -w /work \
  -e CANARY_BASE_URL=https://rc.learn.mit.edu \
  "mcr.microsoft.com/playwright:$TAG" \
  bash -c "npm ci && npx playwright test specs/<property> --project=webkit"
```

Copy the tree rather than mounting it, so the container's `npm ci` cannot overwrite the
host `node_modules`. For a signed-in journey, pass credentials with `--env-file` rather
than `-e`: a `-e` value is visible in the host process list.

WebKit is roughly twice Chromium's wall-clock on the `mit-learn` suite (6.4s against
3.2s for the same three journeys), and that is the point — the extra time is hydration,
which is where the races are.

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
