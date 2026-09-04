# Canaries

Playwright **canaries** for MIT Open Learning web properties: short, browser-driven
user journeys — log in, search for a course, reach a checkout — run continuously by
Concourse against live environments to detect that a real user's path through the
product is broken.

"Canary" here is used in the AWS CloudWatch Synthetics sense: a scheduled, scripted
user journey, not a canary *deployment*.

This is the only JavaScript in `ol-infrastructure`. It lives here, next to the
pipeline that runs it, deliberately — see
[ADR 0011](../../../../docs/adr/0011-playwright-canary-specs-in-ol-infrastructure.md).

## What belongs here, and what does not

| | |
|---|---|
| **Belongs here** | A journey a real user takes, that we want to know about within minutes of it breaking in a deployed environment. |
| **Does not belong here** | Tests that gate a pull request. Those live in the application's own repo, run against its own dev stack, and block merges. |

The distinction matters because the two have opposite failure economics. A PR test
should be strict: any regression must block a merge. A canary should be *stable* —
it wakes a human up, so it must fail only when something is genuinely wrong for
users. A canary that asserts on CMS copy or a course price will page someone every
time an editor changes a word.

**Assert on the journey completing, not on the content it finds along the way.**

`mitodl/mit-learn` has its own Playwright suite at `e2e/` that can be pointed at RC.
It is a good application test suite and deliberately not reused here; ADR 0011
explains why.

## Related, deliberately not duplicated

Grafana Synthetic Monitoring
([`synthetic_monitoring.py`](../../../ol_infrastructure/infrastructure/grafana_alerting/metric_rules/synthetic_monitoring.py))
already runs plain HTTP availability probes against these same properties. It answers
"is the endpoint responding?". These canaries answer "can a person still log in and
find a course?". Alerting for the two is kept separate on purpose.

## Layout

```
meta.py                The canary-meta pipeline. `canary_names` is the source of
                       truth for which canaries are actually deployed.
pipeline.py            CanaryParams + the renderer for one canary-<property>
                       pipeline. `pipeline_params` is a superset of canary_names.
playwright.config.ts   Shared config. Requires CANARY_BASE_URL. Declares one
                       project per browser; CanaryParams.browsers selects.
package.json           Pins @playwright/test. Single source of truth for the
                       container image tag the pipeline pulls.
specs/<property>/      One directory per web property.
  mit-learn/
```

## How these run in Concourse

`canary-meta` manages the fleet and re-sets itself. Each managed pipeline is named
`canary-<property>` and holds a single job that pulls the stock Playwright image,
runs `npm ci`, and runs that property's specs on a `time` trigger.

```bash
cd src/ol_concourse/pipelines/canaries
python meta.py && fly -t pr-inf sp -p canary-meta -c definition.json
```

After that first manual set, `canary-meta` updates itself and every canary pipeline
from `main`.

Onboarding a property is **two list edits**, the same shape as
[`simple_pulumi`](../infrastructure/simple_pulumi/):

1. a `CanaryParams` entry in `pipeline.py`, and
2. its name in `canary_names` in `meta.py`.

The registry is deliberately the wider of the two, so a canary can be added and
reviewed before it starts running against a live property. **Adding a journey to a
property that already has a canary needs neither edit** — `spec_paths` defaults to the
whole `specs/<property>/` directory.

## Which Concourse instance

The production instance (`pr-inf`), and there is deliberately no `--env` switch of the
kind [`simple_pulumi/meta.py`](../infrastructure/simple_pulumi/meta.py) carries.

That switch exists there because some Pulumi stacks run `local.Command` resources
needing VPC-level access to QA or CI infrastructure, so the pipeline has to run *inside*
that network. A canary has the opposite requirement: it is meant to see the property the
way a member of the public does. Every current target — including `rc.learn.mit.edu` — is
publicly reachable, so driving them from one instance is both sufficient and more
faithful to what the canary claims to measure.

A canary against an internal-only endpoint would break that assumption and need the
`--env` treatment: a `canary_names` split per instance and an `extra_args` passthrough on
the self-update job, copied from `simple_pulumi`. Note that such a canary is also no
longer measuring the public user journey, which is worth questioning before adding the
machinery.

## Running a canary locally

```bash
cd src/ol_concourse/pipelines/canaries
npm install
CANARY_BASE_URL=https://rc.learn.mit.edu npx playwright test specs/mit-learn
```

Or in the same image Concourse uses, which is the only way to reproduce a pipeline
failure exactly:

```bash
cd src/ol_concourse/pipelines/canaries
docker run --rm -it --ipc=host \
  -v "$PWD":/specs -w /specs \
  -e CANARY_BASE_URL=https://rc.learn.mit.edu \
  mcr.microsoft.com/playwright:v1.62.1-noble \
  bash -c 'npm ci && npx playwright test specs/mit-learn'
```

## Adding a canary

See [`AGENTS.md`](AGENTS.md) — written for both human and agent contributors, and the
authoritative guide.
