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
playwright.config.ts   Shared config. Requires CANARY_BASE_URL.
package.json           Pins @playwright/test. Single source of truth for the
                       container image tag the pipeline pulls.
specs/<property>/      One directory per web property.
  mit-learn/
```

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
