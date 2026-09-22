# Canary release gate — break-glass runbook

What to do when a production promotion is blocked by the Playwright canary gate,
including how to promote anyway when that is the right call.

**The short version.** The job `deploy-ol-application-<app>-production` failed in
its first task, `<app>-rc-canary-gate`, and the task output ends with the exact
`aws s3 cp` command to authorise the promotion. Run it, then re-trigger the job.
Everything below is why, and when not to.

## What the gate does

`<app>-rc-canary-gate` runs before anything is deployed. It asks one question:

> Did the canary pass against **the release this job is about to promote**, on RC?

It answers it by looking for a single object:

```
s3://ol-eng-artifacts/canary-verdicts/<app>/RC/<version>/pass.json
```

`<version>` is the release calver the job already loaded into `((.:image_tag))`,
so the check is pinned to the release being promoted. A green canary run against
the *previous* release does not open the gate — that stale-green case is the
specific thing this design exists to prevent.

Three outcomes:

| What the gate finds | What it does |
| --- | --- |
| `pass.json` | Promotes. Also warns if a `fail.json` exists for the same release. |
| `fail.json` and no pass | Blocks, and says the canary tested this release and it failed. |
| Neither | Blocks, and says no canary verdict exists for this release at all. |
| `break-glass.json` with a non-empty `reason` | Promotes, printing the override. Checked first. |

Today only `mit-learn` is gated (`AppPipelineParams.gate_production_on_rc_canary`).

## First: which failure is this?

The gate's own output distinguishes them and it is worth reading before acting.

**"The canary FAILED against `<version>`".** The canary ran your release on RC and
a user journey did not work. Default assumption: the release is broken. The
verdict body names the run's `results.json`; the trace, screenshots and video for
that run are under `s3://ol-eng-artifacts/canary-results/canary-<app>/`, and
`trace.zip` opens in <https://trace.playwright.dev/>. Look before you override.

Known false-positive sources, none of which are common:

- The `mit-learn` search journey retries a keystroke against a page that has not
  hydrated. It is the one journey with real variance, and it is RC's latency
  rather than the release.
- An empty or half-rebuilt RC search index makes search journeys fail. This is a
  real failure of RC, and often not of the release.
- The canary's Keycloak account can be locked out. Realm `olapps` on QA/RC is
  `permanentLockout=true`, so a credential drift disables the account rather than
  rate-limiting it, and every subsequent run fails at login. If the failure is at
  login, this is the first thing to check — and the gate is not your problem, the
  canary is.

Measured base rate, from a 10.5-day soak: 8 hard failures in 1,360 runs (99.41%
green), and some fraction of those were genuine RC breakage rather than noise. So
a blocked promotion is much more likely to be telling you something than not.

**"No canary verdict exists for `<version>`".** Nothing tested this release. Every
canary run records a verdict for whatever release it finds on RC — not only
deploy-triggered runs — so a release that has been on RC for longer than one
canary interval (10 minutes for `mit-learn`) should have one. If it does not:

- Is `canary-<app>` paused, or is its job failing before any test runs? Check
  <https://cicd.odl.mit.edu/teams/infrastructure/pipelines/canary-mit-learn>.
- Was the RC deploy of this exact version actually completed? The verdict's
  version comes from the deploy marker the RC job writes, so an RC deploy that
  never finished leaves the canary reporting the previous release.
- Is the version being promoted the version that went to RC? They come from the
  same image tag by construction, so a mismatch means something unusual happened.

Waiting ten minutes is frequently the whole fix, and it is a better fix than an
override.

## Breaking glass

Use it when the promotion has to happen despite the gate: an incident where the
release *is* the fix, a canary that is itself broken, or a failure you have
looked at and understood to be unrelated to the release.

The gate prints this command with the version already filled in. It is also
generated from `break_glass_command()` in
`src/ol_concourse/pipelines/canary_verdicts.py`, so what follows and what the gate
prints cannot drift:

```bash
aws s3 cp - s3://ol-eng-artifacts/canary-verdicts/mit-learn/RC/2026.9.21.1/break-glass.json <<'JSON'
{"reason": "SEV-1: this release is the fix for the outage; canary failure is the locked-out canary account, not the release",
 "who": "your-kerb", "when": "2026-09-21T18:40:00Z"}
JSON
```

Then re-trigger `deploy-ol-application-<app>-production` (`fly -t pr-inf
trigger-job -j <app>-pipeline/deploy-ol-application-<app>-production`, or the
button in the web UI). The gate finds the override, prints it into the build log,
and promotes.

Four properties of this override, all deliberate:

- **It needs only AWS access**, not `fly` access and not a pipeline re-set. The
  people who can promote can override.
- **`reason` is enforced.** The gate refuses an override whose `reason` is missing
  or empty. The object is the only durable record of the decision — the build that
  acted on it is reaped within days.
- **It is scoped to one release version.** It cannot leak into the next promotion,
  and there is nothing to remember to turn back off. The next release gets the
  gate again automatically.
- **It is visible.** It prints into the build log, and it stays in the bucket next
  to the verdict it overrode.

To see what has been overridden recently:

```bash
aws s3 ls --recursive s3://ol-eng-artifacts/canary-verdicts/ | grep break-glass
```

## Turning the gate off

If the gate is wrong often enough to be a problem, the fix is not a standing
override. Set `gate_production_on_rc_canary=False` on that app's
`AppPipelineParams` in
`src/ol_concourse/pipelines/infrastructure/k8s_apps/pipeline.py` and merge it; the
k8s-app meta pipeline re-renders the app's pipeline and the gate task disappears.
That is a reviewable change that says the gate was turned off, which a series of
break-glass objects does not.

## Turning the gate on for another app

Both halves are required, and the Pydantic validator refuses one without the
other:

1. `publish_rc_deploy_marker=True` — the RC deploy job announces the release, which
   is what gives a canary run a release version to key a verdict on.
2. `gate_production_on_rc_canary=True` — the production deploy job reads it back.

The app also needs a canary with `deploy_trigger` set in
`src/ol_concourse/pipelines/canaries/pipeline.py`; see that directory's `AGENTS.md`.
Before merging, confirm verdicts are actually being written for the release
currently on RC:

```bash
aws s3 ls --recursive s3://ol-eng-artifacts/canary-verdicts/<app>/RC/
```

An empty listing means the first promotion after the gate lands will block.
