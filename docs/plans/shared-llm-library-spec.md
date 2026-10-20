# Spec: shared LLM library (packaging, error contract, attribution schema)

Tracked as workflow project `wp-platform-supported-llm-capabilities-ecca0b`, library epic
`tk-shared-llm-library-every-call-site-wrapped-opik--a74a5e`. Each numbered part answers one
decision task and stays Proposed until the decision is recorded here.

| Part | Decision task | Status |
| --- | --- | --- |
| §2 Packaging, home, footprint | `tk-decide-the-shared-library-s-packaging-home-repo--451af4` | Decided: home 2026-09-11 (`mitodl/ol-llm`); SDK-based Opik integration and translation-plugin adoption 2026-10-01 (§2.3) |
| §3 Error contract | `tk-policy-error-contract-for-llm-calls-d27bd1` | Decided 2026-09-11 |
| §4 Attribution schema | `tk-define-the-attribution-schema-shared-by-gateway--d6f26b` | Decided 2026-10-01, with a platform-wide key for anonymous ids (§4.3) |
| §5 Memory | `tk-memory-for-the-shared-llm-pattern-supported-stor-063935` | Categories decided 2026-10-01: a shared service, not coupled to any app (§5.5); backend PoCs open |
| §6 Call contract | `tk-specify-the-library-s-call-contract-timeouts-ret-badb39` | Specified 2026-10-01 from the §3 decision |
| §7 Durable execution | `tk-evaluate-dbos-and-temporal-for-durable-agent-wor-819e78` | Direction set 2026-10-01; nothing decided |

"Inventory" and "direction assessment" below are two internal survey pages dated 2026-09-10
that are not in this repository, and `tk-` ids are tasks in the team's internal tracker. Facts
taken from them are attributed to them; they could not be re-read by a reviewer of this file.

Settled inputs (2026-09-10): every LLM call site goes through the shared library, which owns
the Opik integration. PydanticAI is the default construction path but not mandatory, and a
thin traced-completion path covers single calls. Opik is the platform investment, LangSmith is
retired, and production Opik becomes the shared instance.

Repos were read at learn-ai 4674129, mit-learn f9da62b17, open-edx-plugins e8febd0,
ol-data-platform f9145a18d, ol-django 0eb7f6e, gwarek ee8eaae, and ol-infrastructure 774a26a.
All are dated 2026-09-11 except ol-django (2026-09-08) and gwarek (2026-08-27). Library behavior
was read from installed source of pydantic-ai-slim 2.42.0, opik 2.2.59, and
opentelemetry-exporter-otlp-proto-http 1.44.0, the latest releases on 2026-09-11, plus
comet-ml/opik at `59df1fb9`. §6 was written on 2026-10-01 against pydantic-ai-slim 2.52.0 (openai
3.22.1, anthropic 1.11.0, google-genai 2.26.0, mistralai 3.0.0, boto3 1.43.107), and the §3
facts it depends on were re-checked at that version.

## 1. Consumers

| | learn-ai | mit-learn | open-edx-plugins (translations) | ol-data-platform `ml` | gwarek |
| --- | --- | --- | --- | --- | --- |
| Runs as | Django + Channels + Celery | Django + Celery | Open edX plugin, Celery | Dagster 1.13 | FastAPI + arq |
| Python | 3.14 | 3.12 | edx-platform's: 3.12 on master, 3.11 on ulmo, 3.12 on verawood | 3.14 | 3.13 |
| Client today | LangGraph + ChatLiteLLM | ChatLiteLLM, raw litellm | litellm (`==1.83.0` in course translations) | own factory over openai, anthropic, Bedrock, genai | anthropic SDK |
| Opik / OTel today | opik 2.1.22 with a LangChain tracer; OTel via mitol-django-observability | OTel via mitol-django-observability | Django OTel plugin | since 2026-09-14: opik >=2.2.58 with `@opik.track` and the Prompt Library (`ml/resources/opik_auth.py`); OTel distro baked into the image | none |
| Takes mitol-* from PyPI | yes; `exclude-newer = "7d"` with a `"0d"` override for mitol | yes, same override | no | no (ol-orchestrate-lib as a path dep) | no |

The translation plugins are installed only in mitxonline, which runs edx-platform master
(`src/bridge/settings/openedx/version_matrix.py:33-40`; lehrer `build_manifest.yaml:73-76`).

## 2. Packaging, home repo, and dependency footprint

### 2.1 What the dependency facts rule out

- The full Opik Python SDK is heavy. opik 2.2.59 unconditionally requires
  `litellm` (excluding 1.81.x, 1.82.x, and 1.83.0 to 1.83.6), `pytest`, `sentry_sdk`, and
  `boto3-stubs`, and resolves to 75 packages and about 223 MB. Combined with
  `pydantic-ai-slim[openai]` it forces litellm down to 1.80.17, because litellm 1.100 requires
  `openai<3` while the openai extra requires `openai>=3.8`. It cannot coexist with the
  course-translations plugin's `litellm==1.83.0` pin except by backtracking opik to 2.0.21.
  `import opik` does not load litellm, so the cost is install size and resolver conflicts, not
  import time.
- The Opik SDK reports its own errors to Comet by default. `OpikConfig.sentry_enable`
  defaults to `True` with a Comet-owned Sentry DSN (`opik/config.py`), and `import opik`
  calls `sentry_sdk.init` with that DSN in every process (`SESSION_REPORTING_PROBABILITY =
  1.0` in `opik/error_tracking/api.py`; `opik/__init__.py:131-135`). It hooks the `opik`
  logger and registers an exception hook; events pass Opik's count and status-code filters and
  carry an identifier from `opik.environment.get_user_identifier()`. Any runtime that installs
  the SDK sets `OPIK_SENTRY_ENABLE=false` (settings prefix `opik_`). learn-ai did not set it
  until 2026-09-16, when `main/settings.py:49` began setting it before any opik import
  (learn-ai `f97cae0`). What follows is what was found on 2026-09-11, before that fix.
  This is not only a data-handling issue. `sentry_sdk.init` replaces the global client
  (`sentry_sdk/_init_implementation.py:56-57`), so an app that initializes its own Sentry and
  then imports opik ends up reporting through Opik's client, with Comet's DSN and
  `default_integrations=False`. learn-ai did exactly this: `settings.py` initializes its
  Sentry, then `AppConfig.ready()` imports opik (`main/opik_keycloak_auth.py:145`). As read
  from its Sentry project on 2026-09-11, it had received no request-time error from any
  production release since v0.35.2 (2026-08-05), the first with the Opik wiring. v0.35.2 reported nothing, and v0.35.3 through
  v0.36.1 reported only one startup-time OpenTelemetry log (LEARN-AI-MX). v0.34.0 reported
  about 5,800 events across request-time issues, not counting one connection-error issue with
  313,854. The errors did not stop: production logs on 2026-09-11 include a `django.request`
  500 on `/api/v0/problem_set_list/` and repeated asyncio "Unclosed client session" errors
  (the LEARN-AI-B6 signature), none of which reached the Sentry project. The library
  therefore sets `OPIK_SENTRY_ENABLE=false` before opik is first imported (§2.2). The
  learn-ai fix was `tk-learn-ai-s-sentry-client-is-replaced-by-the-opik-03d28c`; whether
  errors reach its Sentry project again was not re-checked here. With that setting no Sentry
  client is initialized (`sentry_sdk.get_client().dsn` is `None`).
- Comet ships no lighter Python distribution. `opik`'s only extra is `proxy`, which adds
  FastAPI and uvicorn. Upstream opik#4633 ("Optional extras for heavy dependencies", open since
  2026-01-06) got a maintainer answer on 2026-01-08: planned for the next major release
  because it is breaking. 2.x shipped without it, and a 2026-05-15 follow-up has no reply. A
  contributor proposed a separate `opik-core` package in the same thread. opik#4740 ("Make
  litellm optional") was closed the day it was opened.
- The SDK does run without its heavy dependencies. A uv project that depends on
  `opik==2.2.59` and sets `litellm`, `pytest`, `tree-sitter*`, `watchfiles`, `rapidfuzz`,
  `boto3-stubs`, `openai`, and `click` to a never-true marker in `override-dependencies`
  installs 33 MB. `@track` with `opik_context.update_current_trace` ran and queued its trace,
  and litellm, pytest, tree-sitter, and openai were never imported. `sentry_sdk` and `rich`
  are still required at import. Limits: the overridden packages stay in `uv.lock`, so
  dependency scanners keep reporting them; evaluation and LLM-judge metrics need litellm and
  are unavailable; the prompt client imports but a fetch was not exercised; and nothing stops a
  future release from importing a dropped package eagerly, as opik#4740 reports happened after
  1.8.102.
- OpenTelemetry is cheap. `opentelemetry-sdk` plus `opentelemetry-exporter-otlp-proto-http`
  is 14 packages and 7.5 MB, and is already present in learn-ai, mit-learn, open-edx-plugins,
  and the ol-data-platform image. Opik ingests OTLP, so tracing does not need the Opik SDK.
- pydantic-ai-slim is small. 17 packages with no extras; each provider extra adds 4 to 17.
  Its base dependencies include `httpx2` (not `httpx`) and `genai-prices`, so consumers will
  carry both httpx and httpx2. The full `pydantic-ai` package is 99 packages because it pulls
  logfire. Depend on the slim package only.
- Open edX ulmo and verawood cannot take a PydanticAI OpenAI model. Both cap
  `openai<=0.28.1` in `requirements/constraints.txt`; master resolves everything. The
  translation plugins run only on master today, so this blocks nothing now, but the library
  cannot run inside mitx or xpro edxapp until those move off verawood and ulmo.

### 2.2 Design

Home: a new `mitodl/ol-llm` repository. Decided 2026-09-11. Not ol-django, because the
library must not read as Django-bound, and it may gain a TypeScript package. Not agent-kit,
because its scope is agent tooling.

agent-kit was considered. It is a uv workspace of five members (witan-council,
witan-code, witan-core, agent-config-kit, ol-agent-kit), each at `requires-python >=3.11` with
its own Trusted Publishing workflow, so the release mechanics exist. Against it: its scope is
agent tooling (shared memory, code graph, skills, MCP servers), so Dagster code locations,
edxapp plugins, and a FastAPI service would take a runtime dependency from an agent-tooling
repo, and the library would share one workspace lockfile and CI with the witan services.
agent-kit's per-package publish workflow is the template for `ol-llm`'s release job.

- Layout: `python/` holds the `ol-llm` distribution (import `ol_llm`); `typescript/` is
  reserved. The language-neutral contracts (§3, §4, and the prompt fetch semantics) live in
  `docs/` and bind both languages.
- Naming follows mitodl's framework-neutral PyPI libraries (`ol-agent-kit`, `ol-parliament`,
  `ol-concourse`) rather than the `mitol-django-*` family.
- Release: reuse what exists in-house rather than invent: hatchling, calver, and PyPI
  Trusted Publishing when a version bump merges. agent-kit's publish workflow is the one to
  copy; ol-django's (`scripts/release.py:23-35`, `.github/workflows/ci.yml:109-193`, with
  bumpver and scriv) is the other example. CI covers Python 3.11 to
  3.14; ol-django's matrix stops at 3.13, and learn-ai and ol-data-platform run 3.14.
- learn-ai and mit-learn exempt internal packages from their 7-day `exclude-newer` window by
  name in `[tool.uv.exclude-newer-package]`, so each adds an `ol-llm = "0d"` line to take
  releases the day they ship.

TypeScript is reserved, not built. No production JS/TS LLM call site exists today
(inventory, 2026-09-10). When one appears, the TypeScript package implements the same
contracts on OpenTelemetry JS or the Opik TypeScript SDK (npm `opik` 2.2.59, which depends on
the Vercel AI SDK `ai` and `@ai-sdk/*` providers), with the same attribution fields and error
classes. There is no PydanticAI counterpart in TypeScript; picking the client layer is part of
that work.

Packages

| Package | Import | Depends on | Holds |
| --- | --- | --- | --- |
| `ol-llm` | `ol_llm` | `opik` (pinned), `opentelemetry-sdk`, `opentelemetry-exporter-otlp-proto-http`, `httpx` | What every call site needs whatever it is built on: tracing wiring (SDK root trace, OTel spans linked by `OpikSpanProcessor`, Keycloak-refreshing auth), §4 attribution, the §3 policy values (`resolve_policy`), prompt fetch through the SDK with embedded fallback, batch helpers |
| `ol-llm[pydantic-ai]` | | `pydantic-ai-slim` | Model factory, the §6 call contract's retry wrapper, both entry points (agent and traced completion) |
| `ol-llm[openai]`, `[anthropic]`, `[bedrock]`, `[google]`, `[mistral]` | | the matching `pydantic-ai-slim` extra, so each also installs PydanticAI | Provider SDKs, chosen per consumer |
| `ol-llm[harness]` | | `pydantic-ai-harness` (0.x, pinned to a minor series) | In-session memory and compaction (§5 categories A and B). Each harness release pins `pydantic-ai-slim` exactly, so this extra fixes the PydanticAI version |
| `ol-llm[memory]` | | `hindsight-client` | Client for the shared memory service (§5.5). Provisional until the Hindsight PoC reports |
| `ol_llm.eval` (module) | `ol_llm.eval` | the full opik install, including litellm | Datasets, experiments, `evaluate(prompts=…)`, LLM-judge metrics. Runs in CI eval jobs and on developer machines, never in slimmed runtime images |
| `ol-llm[django]` | `ol_llm.django` | Django | Settings reader, app-ready tracer setup, Celery helpers. Add only when the Django glue outgrows a settings reader |

Extras added 2026-10-02 at the project owner's direction: PydanticAI is the default but not
mandatory (settled 2026-09-10), so a LangGraph or plain-SDK call site installs the base
package alone, and CI checks that the base imports with no extra present. Durable-execution
extras (`temporal`, `dbos`) wait for §7's evaluation.

No Dagster adapter package. The batch helpers in the core are framework-neutral, and the
Dagster resource that wraps them belongs in ol-orchestrate-lib until a second Dagster
consumer exists.

Opik integration: the SDK, slimmed per consumer. Revised 2026-09-11 after weighing what a
REST reimplementation would have to rebuild. The prompt client in `opik/api_objects/prompt/`
is 2,415 lines: environment and version lookup, a locked per-entry TTL cache with a background
refresh thread that keeps the stale value on failure, invalidation when a label moves, chat
and text templates, and attachment of the fetched version to the current trace. Rebuilding
that is the larger cost, and the native prompt link comes with it. None of that code imports
a dependency the §2.1 overrides drop.

So the core uses the SDK for prompts and for the root trace of each run (`@opik.track`), and
links PydanticAI's OTel spans into that trace with `opik.integrations.otel.OpikSpanProcessor`.
The processor exists for this case: its docstring describes attaching the root OTel span of a
library that emits its own spans ("e.g. logfire / PydanticAI") to the surrounding
`@opik.track` context. PydanticAI is instrumented with
`Agent(capabilities=[Instrumentation(settings=InstrumentationSettings(tracer_provider=…))])`
(`Agent(instrument=…)` raises `TypeError` in 2.42), and its spans travel over OTLP/HTTP to
`/api/v1/private/otel`. The exporter takes a `requests.Session` (`OTLPSpanExporter(session=…)`),
so the Keycloak client-credentials refresh attaches as the session's auth. SDK REST calls use
the same token source through `opik.hooks.add_httpx_client_hook`, as learn-ai does today
(`main/opik_keycloak_auth.py`). The traced-completion path uses `pydantic_ai.direct` under the
same root trace.

Verified 2026-09-11 in a scratch project with opik 2.2.59, opentelemetry-sdk 1.44.0,
pydantic-ai-slim 2.42.0, and the §2.1 overrides: a `@track` function running an instrumented
PydanticAI agent (`TestModel`) with `OpikSpanProcessor` on the tracer provider completed, the
install was 67 MB, and litellm, pytest, tree-sitter, and openai were never imported.

Verified end to end 2026-10-01 against a local Opik 2.2.87 (upstream docker compose, no
Keycloak in front), with opik 2.2.87, pydantic-ai-slim 2.52.0, opentelemetry-sdk 1.45.0, and
the same overrides (46 packages):

- The `@track` root trace arrived with its tags, metadata, `thread_id`, and
  `environment="qa"`.
- PydanticAI's spans, exported over OTLP/HTTP to `/api/v1/private/otel/v1/traces`, landed in
  the same trace with the right parents: `tutor` (the tracked function), then
  `invoke_agent agent`, then `chat test` typed `llm` with model, provider, and token usage.
- `get_prompt(name, environment="production")` returned v1 while v2 was the latest, and the
  fetch inside the tracked function put the prompt id, commit, and version number in the
  trace's `opik_prompts` metadata.
- litellm, pytest, tree-sitter, openai, and click were never imported.
- Trace input and output, and the model request messages on the `llm` span, are captured by
  default.

Two behaviors the prompt helper has to handle:

- With Opik unreachable and a cold cache, `get_prompt` raised `httpx.ConnectError` after
  11 s. The helper needs its own short deadline around the first fetch before it falls back
  to the embedded default.
- Moving a label to an unregistered environment name fails
  (`EnvironmentNotFoundError` for `ci`). Only `development`, `staging`, and `production` are
  seeded, so `ci` and `qa` have to be created once per instance.

Not verified: the Keycloak-refreshing auth on the OTLP exporter session, since the local
instance has no auth in front of it. learn-ai and ol-data-platform both run the SDK-side
hook (`opik.hooks.add_httpx_client_hook`) against the deployed instances today.

Consequences:

- Slimming is consumer configuration; the library cannot impose it. uv consumers copy the
  §2.1 `override-dependencies` block, which the library documents and exercises in CI.
  pip-based consumers (edxapp) get the full SDK. On edx-platform master the full set resolves
  once the translation plugins drop their `litellm==1.83.0` pin, which adoption does anyway.
- The library calls `os.environ.setdefault("OPIK_SENTRY_ENABLE", "false")` in its package
  `__init__` before importing opik, so no consumer can repeat the learn-ai Sentry bug.
- Evaluation and LLM-judge metrics need litellm, so `ol_llm.eval` runs only where the
  overrides are absent: CI eval jobs and developer machines, as a separate uv project.
- The library pins opik exactly, and its CI runs the import-and-trace smoke test in the
  slimmed shape on every opik bump, since a release could start importing a dropped package
  eagerly (opik#4740).
- The fallback, if the slimmed shape breaks and upstream does not fix it, is OTel-only tracing
  plus a REST prompt client with metadata-only prompt linkage. The prompt-overrides decision
  (`tk-decide-prompt-overrides-cache-behavior-fallback--5e7c55`) still sets the embedded
  default and fallback marking either way, since `get_prompt` raises on a cold start with Opik
  unreachable.

litellm is not a dependency. mit-learn's non-chat uses of litellm (token counting,
`litellm.embedding`) stay in mit-learn until the library's embedding entry point exists.
Embeddings come under the library after the chat core (decided 2026-10-01,
`llm-usage-policies-spec.md` §4.1).

Python floor `>=3.11`, the oldest Python among edx-platform releases in use (ulmo).
pydantic-ai-slim and the OTel packages require `>=3.10`, so the floor costs nothing and keeps
the non-OpenAI paths installable on ulmo.

Versioning: calver; consumers take updates through Renovate. ol-data-platform and gwarek
do not use `exclude-newer`, so they need no override.

### 2.3 Decisions (2026-10-01)

1. The library uses the SDK-based Opik integration above: the SDK for prompts and root
   traces, slimmed per consumer. It works end to end against a local Opik with no Keycloak
   in front and a test model (§2.2); the authenticated OTLP path is not yet verified. Both
   existing Opik consumers already install the SDK, learn-ai through the LangChain
   `OpikTracer` and ol-data-platform through `@opik.track` and the Prompt Library, though
   neither uses `OpikSpanProcessor` or the slimming overrides. Upstream still has no slim
   distribution:
   opik 2.2.87 hard-requires litellm, openai, pytest, and boto3-stubs, and opik#4633 is open
   with no maintainer commitment (last comment 2026-09-15). The OTel-plus-REST shape stays on
   file as the fallback if a release breaks the slimmed install.
2. The translation plugins adopt on master-only mitxonline. They cannot then be installed on
   ulmo or verawood edxapp until those releases move off `openai<=0.28.1`.

With the home decided on 2026-09-11, this closes the packaging decision. The repository and
package skeleton are `tk-create-mitodl-ol-llm-with-the-ol-llm-package-ske-89b2db`, and this
part moves to `ol-llm`'s `docs/` when that exists.

## 3. Error contract

The policy every call site follows. The call-contract task
(`tk-specify-the-library-s-call-contract-timeouts-ret-badb39`) turns it into types and
defaults.

### 3.1 What exists today

No integration has a timeout on every model call, and none has a fallback model. Both
translation plugins decide retryability by substring on the error message. ol-data-platform
skips failed rows, checkpoints chunks, and trips a circuit breaker after one fully failed
chunk. learn-ai appends error text to the chat stream; mit-learn's summarizer returns status
strings; the course-translations plugin silently keeps source text when a segment ID is
missing (inventory, 2026-09-10).

### 3.2 Rules

1. Every model request has a timeout, from one standard shape that operators can override
   by environment variable. The library refuses to build a model without one.
   `ModelSettings.timeout` covers OpenAI, Azure OpenAI, Anthropic, Google, and Mistral. Bedrock
   ignores it and takes `BedrockProvider(aws_read_timeout=, aws_connect_timeout=)`, so the
   factory sets both from the same value. Defaults:

   | Profile | `TIMEOUT_SECONDS` | `MAX_RETRIES` | `MAX_BACKOFF_SECONDS` |
   | --- | --- | --- | --- |
   | Request-time | 60 | 2 | 10 (total) |
   | Batch | 300 | 5 | 300 (Retry-After honored up to this) |

   Resolution order, later wins:

   1. the profile default above;
   2. a use-case default declared in code, with a written reason (mit-learn's credential
      metadata keeps its documented 120 s this way);
   3. `OL_LLM_<PROFILE>_<SETTING>`, e.g. `OL_LLM_BATCH_TIMEOUT_SECONDS`;
   4. `OL_LLM_<USE_CASE>_<SETTING>`, e.g. `OL_LLM_COURSE_TRANSLATION_MAX_RETRIES`.

   The environment prefix follows the package name. The effective values are recorded on the
   trace as `opik.metadata.*` so an override is visible when debugging.
2. Retry transient failures only, classified by type and status, never by message text.

   | Condition (PydanticAI 2.42) | Class | Retry | Fall back |
   | --- | --- | --- | --- |
   | `ModelHTTPError` 429 | rate limited | yes, honoring `retry_after` | yes |
   | `ModelHTTPError` 408, 500, 502, 503, 504, 529 | provider unavailable | yes | yes |
   | `ModelAPIError` that is not `ModelHTTPError` (connection errors; OpenAI, Anthropic, and Bedrock timeouts arrive this way) | provider unavailable | yes | yes |
   | `ModelHTTPError` 400, 401, 403, 404, 422 | rejected (our bug or config) | no | no |
   | `ContentFilterError` | content filtered | no | no |
   | `UnexpectedModelBehavior` after output retries are spent | invalid output | no | no |
   | `UsageLimitExceeded` | budget exceeded | no | no |

   There is exactly one retry layer per provider. The OpenAI and Anthropic SDKs retry twice by
   default underneath PydanticAI and botocore retries four times, so the library sets every
   SDK's retries to zero and retries in one place (§6.3). Google and Mistral raise raw
   `httpx2` transport timeouts rather than `ModelAPIError` (measured 2026-10-01, §6.1); the
   library maps them into the same class.
3. Validation failures retry on the same model, not a fallback. Output validators raise
   `ModelRetry`; `Agent(retries=…)` bounds them (PydanticAI's default is 1 for tools and 1 for
   output). A model that cannot produce the schema is not fixed by a different model mid-run.
4. Fallback is a declared per-use-case policy. Composed as `FallbackModel` with
   `fallback_on` set to the "fall back" rows above, not PydanticAI's default of every
   `ModelAPIError`, which would mask 401s and 400s. Streams fall back only before the first
   chunk (`models/fallback.py:334`); a failure mid-stream reaches the caller. If a gateway
   route does its own fallback, the library policy for that route has none, so retries do not
   multiply.
5. Callers see typed errors. The library raises its own small set (`LLMRateLimited`,
   `LLMUnavailable`, `LLMRejected`, `LLMContentFiltered`, `LLMInvalidOutput`,
   `LLMBudgetExceeded`), each carrying the original as `__cause__`. Request-time surfaces map
   the class to user-facing text; raw provider messages never reach users.
6. Degradation is allowed only when marked. A result produced by fallback, by an embedded
   prompt default, or with parts left untranslated carries a `degraded` marker in the return
   value and `opik.metadata.degraded` plus a reason on the trace. The translation plugins'
   silent keep-source behavior becomes a marked partial result.
7. Batch isolates rows. A row failure records its class and does not fail the chunk;
   rejected-class failures open the circuit immediately, since retrying a config bug burns
   budget; unavailable-class failures open it after a threshold. ol-data-platform's
   summarize module is the reference.

### 3.3 Decision

Decided 2026-09-11: rules 1 to 7 as written, with timeouts, retries, and backoff in one
standard shape that environment variables can override (rule 1). Fallback is opt-in per use
case; the timeout and retry rules are mandatory.

## 4. Attribution schema

Every LLM call carries the same fields so cost, quality, and traffic join across Opik traces,
gateway logs, and Prometheus. The library applies them; call sites supply values.

### 4.1 Fields

| Field | Required | Example | Opik (OTel) | Gateway (APISIX) | Prometheus label |
| --- | --- | --- | --- | --- | --- |
| `app` | always | `learn-ai` | `project_name` on the root trace, and tag `app:learn-ai` | consumer name (one consumer per app) | `consumer` (gateway), `app` (library) |
| `use_case` | always | `tutor`, `course-translation`, `feedback-summary` | tag `use_case:…`, `opik.metadata.use_case` | request header `X-OL-LLM-Use-Case`, added to the `llm_*` metrics as an extra label (§4.3) | `use_case` (gateway and library) |
| `environment` | always | `ci`, `qa`, `production` | native `environment` on the root trace, plus tag `env:…` | implicit (one gateway per environment) | existing environment label |
| `model`, `provider` | always | `gpt-4o-mini`, `openai` | `gen_ai.request.model`, `gen_ai.provider.name` (emitted by PydanticAI) | `llm_model`, `request_llm_model` | `llm_model` (gateway), `model` and `provider` (library) |
| `prompt_name`, `prompt_version` | when a managed prompt is used | `tutor-system`, `7` | native prompt link (the SDK attaches a prompt fetched inside the root trace), plus `prompt_name` and `prompt_version` in metadata | none | none |
| `user_id` | request-time with a user | pseudonymous id, see 4.2 | `opik.metadata.user_id` | OpenAI `safety_identifier` or `user`, Anthropic `metadata.user_id` → `llm_end_user_id` | never |
| `thread_id` | conversations | chat thread id | `thread_id` on the root trace; `gen_ai.conversation.id` via `run(conversation_id=…)` on spans | none | never |
| `run_id` | batch | Dagster run id, Celery task id | `opik.metadata.run_id` | none | never |
| `degraded` | when true | `true` plus a reason | `opik.metadata.degraded`, `opik.metadata.degraded_reason` | none | `degraded` counter (library) |

Conventions: snake_case keys. Tags carry only low-cardinality `key:value` pairs (app,
use_case, environment) for filtering; everything goes in `opik.metadata.*`. Prometheus labels
never carry user, thread, or run identifiers.

Where the library writes them. The root of every run is an `@opik.track` trace, so the
project, tags, metadata, `thread_id`, and Opik's native `environment` field are set through the
SDK (`@track(project_name=, tags=, metadata=, environment=)` and
`opik_context.update_current_trace`). PydanticAI's OTel spans become children of that trace
through `OpikSpanProcessor` (§2.2). PydanticAI's own `metadata=` lands as a single JSON
attribute on its `invoke_agent` span, which Opik does not unpack, so the library does not rely
on it.

Environment on the OTel-only fallback. Opik's OTel mapping has no rule for the trace
environment field. Anything that reaches Opik over OTLP alone, which is only the §2.2 fallback,
carries environment as the `env:…` tag and `opik.metadata.environment`. Prompt environment
labels are a separate mechanism and are unaffected.

Model names must match Opik's price table. Opik computes cost from model name and token
usage. opik#5620 (an incoming `gen_ai.usage.cost` was dropped) was closed on 2026-09-18 and
the mapping rule is in the backend at `3de1c5e` (`GenAIMappingRules.java:65-66`); whether the
deployed chart (2.2.81) carries it is not verified. Either way the SDK root trace is priced
from the model name, so the model catalog policy
(`tk-policy-model-catalog-and-lifecycle-ba2fc7`) must use names Opik prices, or add prices to
the backend image. ol-data-platform already works around a mismatch by hand
(`infer_llm_provider`, `dg_projects/ml/ml/resources/opik_auth.py:206-213`).

Online scoring rules cannot filter on the environment field. Rule filters handle metadata
and tags; `ENVIRONMENT` falls through to "Unsupported trace field"
(`TraceFilterEvaluationService.java:71-95` at `3de1c5e`). The `env:…` tag is therefore what
scopes a rule to production traces, not the native field.

### 4.2 User identifiers

`user_id` is a pseudonymous, stable identifier that is not a credential and not directly
identifying: the app's global user id (learn-ai uses `user.global_id`) for signed-in users,
and for anonymous users a keyed hash of the session identifier, prefixed `anon-`. Emails,
usernames, and raw session keys never leave the app. The hash key is one platform-wide key
held in Vault and delivered to every app (decided 2026-10-01, §4.3).

learn-ai implements this as of 2026-09-16
(`tk-learn-ai-sends-anonymous-users-raw-django-sessio-16bd34`): `get_trace_ident()` returns
`user.global_id` or `anonymize_ident(get_ident())` (`main/consumers.py:43-57` at `9f29bda`),
and `anonymize_ident` is HMAC-SHA256 keyed with the app's `SECRET_KEY`, prefixed `anon-`
(`main/utils.py:27-44`). The schema takes that prefix and that construction as the
convention; the key changes from the app's `SECRET_KEY` to the platform key. The raw session
key is still used locally for throttle cache keys, which never leave the app.

### 4.3 Open questions, answered from source 2026-10-01

1. `use_case` on gateway metrics. APISIX can do it without a route per use case:
   `plugin_attr.prometheus.metrics.<metric>.extra_labels` is read for any metric name and each
   value resolves through `ctx.var`, and it is wired into every `llm_*` metric
   (`apisix/plugins/prometheus/exporter.lua:113-133, 571-644` at tag 3.18.0). The shipped
   example config shows it only for the three HTTP metrics, and it has not been run. Proposed:
   the library sends `X-OL-LLM-Use-Case` on every gateway request, the gateway maps it to a
   `use_case` label, and the APISIX PoC confirms it at runtime. Agent Router keys rate limits
   on request headers (`clientSelectors.headers`), so a header is the portable carrier;
   whether its metrics can take the label is for its PoC. The gateway already exposes the end-user id the library sends in the request
   body as `$llm_end_user_id` (`ai-proxy/base.lua:331-334`); it stays out of metric labels.
2. The anonymous-user hash key. learn-ai shipped a per-app key (its own `SECRET_KEY`).
   Decided 2026-10-01: one platform-wide key, so the same anonymous identifier hashes to the
   same id in every app. The key is necessary for that join and not sufficient: each app
   hashes its own Django session key today, and two apps' session keys for one visitor are
   unrelated. Anonymous activity joins across apps only once the hashed input is an
   identifier the apps share. Until then the platform key changes nothing observable.

Decided 2026-10-01: §4.1 and §4.2 are adopted, with item 1 as proposed and item 2 on the
platform-wide key. On routes where no gateway is in the path (the gateway is optional in
phase one, `llm-gateway-requirements.md` §2), the gateway column does not apply and the
library's own metrics carry the same labels.

## 5. Memory

Added 2026-09-15: memory is part of the platform capability set, so the supported storage and
retrieval paths are decided here and encoded in the library rather than left to each app. The
Repository facts in this part carry file references. The claims about third-party products
in §5.4 (cognee, graphiti, zep, letta, mem0, Hindsight, upstream Omnigraph) were read from
their repositories and documentation on the dates given and are not individually cited.

The only prior statement of intent is a one-word bullet, "memory", in the planning repo's
`epics/llmops/platform_investment.md`, a 14-line file in a repo with no commits. The
requirements below are therefore written from scratch.

### 5.1 The five things called "memory"

They have different storage paths, lifetimes, and privacy obligations, and conflating them is
how this decision goes wrong.

| | Category | Lifetime | Today |
| --- | --- | --- | --- |
| A | In-session conversation state | One thread | learn-ai only: LangGraph `AsyncDjangoSaver` writes `DjangoCheckpoint` / `DjangoCheckpointWrite` rows holding full prompts and replies (`checkpointers.py:281,327`) |
| B | Context compaction | One request | learn-ai's `MessageTruncationNode` keeps the last 6 human messages (`api.py:305-384`); mit-learn does tiktoken truncation on a single prompt (`learning_resources/utils.py:807-815`) |
| C | Cross-session recall | Across sessions | None. RFC mitodl/hq#13314 proposes it |
| D | Durable user-scoped facts | Until deleted | None LLM-derived. MIT Learn profile fields are the source of truth in the RFC |
| E | Corpus retrieval (RAG) | Corpus lifetime | mit-learn's Qdrant collections. Out of scope here; it belongs to `tk-policy-scope-of-embeddings-and-mcp-tool-access-i-3d8034` |

### 5.2 Current state (verified 2026-09-15)

- Nothing to migrate. No integration stores user profiles, preferences, or learned facts.
  learn-ai carries a `langmem` dependency and summarization prompts with no node wired to them
  (`chatbots.py:422-427`). The RFC's prototype branch is the only implementation.
- No deletion path. No repo can delete a user's LLM-derived content. Retention and deletion
  belong to the data-handling policy, which is on hold (`llm-usage-policies-spec.md` §3).
- witan is not the substrate. Its only entry points are the CLI and the deployed MCP
  endpoint (agent-kit `mcp/servers/witan/pyproject.toml`; `witan/__init__.py` exports nothing); it has no
  embeddings (`WITAN_EMBED_ENABLED` off by default, no vector column), no per-row ACL, and no
  retention; the omnigraph data tier is ClusterIP-only and exposing it through APISIX was
  rejected in witan's ADR-0005 (agent-kit `mcp/servers/witan/docs/adr/`). Writes serialise per graph, measured p50 33.3 s with 8 concurrent
  writers on a 1,045-row graph. It is agent tooling.
- Infrastructure on hand. Postgres RDS per app, Valkey per app, Qdrant Cloud provisioned
  for mit-learn alone (the in-cluster `qdrant` namespace is empty), OpenSearch, ClickHouse
  (multi-tenant, Opik), S3, and a Glue plus Starburst lakehouse. No graph database runs in
  production. CNPG is local-dev only.

### 5.3 What the library owns, whatever the backend

1. Memory extraction is an LLM call site. The RFC's design makes two model calls per batch
   (a gate model and an extraction model) through `ChatLiteLLM`. Under the settled rule that
   every call site is wrapped, those calls take the §3 contract (timeout, retry classes,
   structured output), carry §4 attribution with a `memory-extraction` use case, are traced in
   Opik, and count against the budgets policy. A memory feature that calls models outside the
   library is the same gap the library exists to close.
2. Content rules are platform policy, not one app's prompt. Never store names, email
   addresses, assessment content (problem statements, answers, hints, grades, identifiers), or
   instructions aimed at the bot's own rules, tools, or permissions. The library provides the
   enforcement point and the evaluation cases; the RFC's list is the starting text.
3. Memory reaches traces. Notes injected into prompts appear in trace content, so
   `InstrumentationSettings.include_content` and Opik access and retention follow the
   data-handling policy. The RFC states this of LangSmith, which is being retired; it
   transfers to Opik unchanged.
4. Deletion is a requirement of the interface. Any supported backend must delete
   everything for one user in one call, and that delete must reach every store it wrote to.
   Cross-service account deletion (mit-learn to learn-ai) is an existing gap and is owned by
   the data-handling policy.
5. Identity matches §4. Memory is keyed on the same pseudonymous user identifier the
   attribution schema uses, taken from the authenticated identity, never from a request body
   or model output.
6. One narrow interface. The library exposes user-scoped get, put, and search so the
   backend can change without touching call sites. The RFC already built that seam as a
   LangGraph `BaseStore` adapter (`DjangoMemoryStore`, namespace `("memories", global_id)`,
   value `{"text": …}`), whose deletion guarantee comes from a Postgres foreign key and whose
   search is deliberately inert, with no embedding index.

### 5.4 Backend options

The project owner's stated preference (2026-09-15) is to incorporate
[cognee](https://www.cognee.ai/) for general-purpose memory. Evaluated 2026-09-15 against
cognee 1.5.4 (released 2026-09-04, Apache-2.0, 30.7k stars, active).

What cognee is good at. A knowledge graph built from ingested documents, with 24 search
types (graph completion, triplets, temporal, Cypher, RAG and hybrid completion), a real
multi-tenant model (`User` with `tenant_id`, `Tenant`, `Role`, dataset ACLs) where every API
call takes a user and retrieval can be constrained to one, and one-call per-user deletion
(`forget(everything=True)` for a user's datasets, `memory_only=True` to drop graph nodes and
embeddings while keeping raw files) that reaches the graph, vector, and relational stores.

What it costs us.

| | Finding |
| --- | --- |
| New infrastructure | Postgres-as-graph is explicitly a demo feature in OSS and a licensed product in production (README.md:235). The default graph provider is the embedded `ladybug` file, which is single-writer and a poor fit for multi-replica Kubernetes, so production means running Neo4j or Kuzu. We run no graph database today |
| Our stores | RDS Postgres works (relational plus pgvector) and Valkey is optional. Qdrant, OpenSearch, ClickHouse and the lakehouse are not supported adapters; there is no Qdrant adapter in `databases/vector/` |
| Its model calls | Made in-process through litellm, so they do not pass through this library. Provider, endpoint, and key are configurable (including `llm_provider="custom"` for an OpenAI-compatible gateway route) with per-stage overrides, so routing is solvable. Tracing is not: the `Observer` enum is `NONE / LLMLITE / LANGSMITH`, with no Opik, though it emits OTel spans honouring `OTEL_EXPORTER_OTLP_*`, so Opik would have to be reached by pointing OTLP at it |
| Ingestion cost | The default pipeline runs entity extraction and summarization per chunk, roughly two structured-output calls per chunk plus an embedding per data point. A ten-chunk document is about twenty model calls. Exact token counts unverified |
| Prompt cost | `search` defaults to `top_k=15`, and graph completion returns expanded triplets and chunks: realistically thousands of tokens per request, against the RFC's ~1,100-token budget for three notes |
| Telemetry | Posts to `https://test.prometh.ai` with user and tenant ids, a machine-persistent id, and a hash derived from the LLM API key, unless `TELEMETRY_DISABLED` is set (`shared/utils.py:21,251,362`) |
| Operational | Migrations auto-run on FastAPI startup and on the first `remember()`/`cognify()` call in an SDK process, across relational, graph, and vector stores. Backup and restore are undocumented. The Helm chart is thin (`replicaCount: 1`, no graph database, `appVersion` 1.16.0 against a 1.5.4 release). 173 PyPI releases since 2024, dev versions published out of order, and LLM-generated changelogs that claim no breaking changes: pin exact versions |
| Fit behind §5.3 | Poor. There is no namespaced key/value get and put. A `put` becomes `add()` plus a `cognify()` run (LLM calls, seconds), and `get` has no equivalent; `search` returns graph-expanded text rather than the value stored. An adapter would be lossy and slow |
| Evidence at our scale | Vendor-published only (Bayer, Knowunity, Wyoming). Their own evaluation report is self-labelled preliminary and concedes the 10M configuration was tuned on the questions it was scored on. Nothing independent at hundreds of memories across tens of thousands of learners |

What it does not do, so these stay ours either way: in-session transcript storage, rolling
summarization of a live chat, token-bounded history, and `ModelMessage` serialization.

Alternatives, for the record. graphiti (Apache-2.0, v0.30.2, 2026-09-08) is the strongest
fully open option and has little cloud gating, but defaults to Neo4j, so it carries the same
new-infrastructure cost. zep's Community Edition was discontinued 2025-04-02 and letta's Python
server is retired ("do not use this repository as a working Letta implementation"); neither is
a candidate. mem0 is active, but whether its OSS tier includes graph memory at all is
unverified and its graph features are gated at a paid tier.

Closer to home. PydanticAI ships first-party memory in a separate 0.x package,
`pydantic-ai-harness` 0.31.0 (2026-09-12): a `Memory` capability with `InMemoryStore`,
`FileStore`, `SqliteMemoryStore` and `PostgresMemoryStore`, a per-run namespace resolver taken
from typed deps and never exposed as a tool argument, plus `compaction` (sliding window,
clear-tool-results, summarizing, tiered) and BM25 `conversation_search`. Search is literal, not
semantic. That covers categories A, B, and the RFC's shape of C on infrastructure we already
run, at 0.x maturity.

Omnigraph. Proposed 2026-09-15 as the storage layer, possibly behind a witan variant or a
separate MCP surface, on the strength of our already running it. Evaluated the same day
against upstream `ModernRelay/omnigraph` (third-party, MIT license, Rust over Lance 11.0.0),
agent-kit, and the deployed stack. It is not viable for learner memory, on four structural
grounds rather than tuning gaps.

- Writes serialise per graph, by design. Upstream's deployment guide states one
  mutation-capable writer per cluster, with read replicas and overlapping writers explicitly
  unsupported; the commit model is a per-manifest compare-and-swap, giving roughly 6 to 7
  commits per second on object storage. Our own measurements agree: p50 33.29 s at 8 concurrent
  writers with throughput _falling_ as writers are added (ol-infrastructure
  `applications/omnigraph/data_tier.py`, the measurements in its header comment), and a QA
  probe at 16 writers returned 0 acknowledged, 16 errored, 8 indeterminate, while 13 writes
  actually landed. Reads are fine on a compacted graph (5 to 11 ms pooled HTTP) but degrade
  with fragmentation: p50 167 ms after 913 sequential inserts, 7.7 ms after `optimize`.
- Per-learner isolation is inexpressible. The partition unit is the graph, boot costs
  about 1.18 s per declared graph with a budget of roughly 110, there are no namespaces, and
  branch creation measured 120 s on production object storage. That forces one shared graph
  with a `learner_id` property. Cedar authority is per-graph and per-branch only, with no
  per-node or per-row scoping, so isolation between learners becomes an application-code
  invariant the store cannot enforce. Identities are static bearer tokens mapped to
  pre-provisioned actor ids; it does not understand JWTs, so a learner can never be a
  principal.
- Erasure has no per-record path. A delete removes the row at head, but the content stays
  readable from any prior commit. Only `omnigraph cleanup` erases; it is destructive,
  direct-storage only (never through the server), cannot be Cedar-gated at all, wants writers
  stopped when our writer is the server, and runs as a weekly job with `--older-than 30d`,
  a 30-day floor. Full erasure also means expiring S3 object versions (the bucket has
  versioning with no noncurrent-version expiry), rebuilding full-text indexes, and breaking
  time-travel and change-feed reads for every other consumer.
- The operational base is thin for learner data. Single replica, `Recreate`, no PVC, state
  in S3; no backups, no cross-region replication, and no documented restore (the team's own
  runbook says reassembling a Lance store from object versions "is not a recovery path anyone
  should be attempting under time pressure"); no metrics endpoint; `/healthz` cannot detect a
  quarantined graph; and nightly `optimize` has wedged a graph four times in fourteen days,
  once for 15 hours.

The access path is also closed today: ol-infrastructure's ADR 0009 keeps omnigraph-server on
the cluster network and witan's ADR-0005(c) explicitly rejected exposing it through APISIX as "a second, policy-unmediated
boundary". An MCP surface does not fix the contract either. Measured tier overhead is about
289 ms per call when idle, rising to roughly 2.46 s under 16 writers, and the tool-call
deadline makes writes indeterminate: in the QA probe the store committed 16 of 16 writes while
15 callers were told their write failed. There is no vendor Python SDK ("coming soon"); what
exists is agent-kit's internal hybrid HTTP-plus-CLI client.

Omnigraph's vector support is genuine (Lance IVF ANN, BM25, and `rrf()` hybrid fusion), but it
is unexercised here: witan runs BM25 only with embeddings disabled, and a Lance analyzer
rebuild once left every `search()` returning `409` for a week.

Hindsight (Vectorize). Evaluated 2026-09-16 against `vectorize-io/hindsight` at HEAD
`4f1b062`, release v0.10.0 (2026-09-14). MIT licensed, genuinely open source, and the
strongest of the three on the criteria that decided the other two.

- No new datastore. Self-hosting needs only PostgreSQL 15+ with pgvector; the graph,
  BM25, vector and temporal search all run inside Postgres. No graph database, no Qdrant, no
  queue. Images on ghcr.io, a Helm chart at v0.10.0 with HPA, PDB and a ServiceMonitor, and
  optional TEI sidecars. Embeddings and reranking default to local in-process models, so the
  full image is about 9 GB; the slim image is about 500 MB with external embedding endpoints.
- Its model calls are redirectable and traceable. Roughly 27 providers are first-class
  config, `HINDSIGHT_API_LLM_BASE_URL` plus per-operation overrides point them at our gateway,
  and `HINDSIGHT_API_LLM_DEFAULT_HEADERS` is documented for operators routing through proxies
  and request-tracing middleware. OpenTelemetry is a first-class dependency (traces default
  off) with Prometheus `/metrics`. There is no Opik integration; Opik over OTLP is plausible
  and unverified. Ingestion costs roughly 5 to 10 LLM calls per conversation (derived from
  defaults, not vendor-published); embeddings stay local by default.
- Deletion is real. One `DELETE /v1/default/banks/{bank_id}` issues actual deletes across
  documents and chunks, attachments, memory units, observation history, entities and their
  links, extension tables, the bank row, and the object-store prefix. Vectors live in those
  rows, so they go too.
- Retrieval fits the budget. `recall` defaults to 4,096 tokens but the cap is a
  per-request parameter, and results over the remaining budget are skipped. For the RFC's
  three-notes shape the better primitive is a mental model, which is a plain database read
  with no retrieval, synthesis or LLM call.
- First-party PydanticAI support: `hindsight-pydantic-ai` 0.4.20 (beta) against
  `pydantic-ai-slim`, exposing retain, recall and reflect as tools. Python client
  `hindsight-client` 0.10.0 is OpenAPI-generated and async.

What we would have to close ourselves, none of it vendor-blocked:

1. `llm_requests` keeps a readable copy of learner content. It stores each call's input and
   output up to 50,000 characters, is enabled by default, and is _not_ deleted by
   `delete_bank`; only an hourly retention sweep clears it, default one day. Set
   `HINDSIGHT_API_LLM_TRACE_ENABLED=false`. `audit_log` is off by default but retains forever
   when enabled.
2. Deletes need fencing against in-flight ingest. Writes auto-create banks
   (`_ensure_bank_exists`), and consolidation reconciles every 300 s, so a queued retain
   landing after a delete silently recreates the learner's bank.
3. Per-learner isolation is not enforced out of the box. Banks are fully isolated and
   retrieval is bank-scoped by path, but in the OSS server the `authorization` header is
   optional, the built-in MCP endpoint is unauthenticated by default, and the bundled API-key
   extension is a single shared key that authenticates the app rather than the learner.
   Enforcement at our scale means writing a custom `TenantExtension`.
4. The LangGraph `BaseStore` adapter was removed in `hindsight-langgraph` 0.2.0; the
   published docs still describe it and are stale. The §5.3 shim becomes our code, thin but
   ours. Note also that `retain` is not a plain write: it runs asynchronous LLM extraction
   unless `retain_extraction_mode: chunks` is used or notes are stored as mental models.
5. Chart defaults are unsafe: a bundled `ankane/pgvector:latest` with the password
   `hindsight`. Disable it and point at RDS.

Cloud is not an option for learner data: Vectorize hosts all infrastructure in the USA with
OpenAI and Groq as named LLM subprocessors and no published region choice. Self-hosting is
fully featured, so this costs us nothing.

The caution is maturity and concentration, not architecture: the repo was created 2025-10-30,
is at v0.10.0, and one contributor accounts for about 58% of commits. Production references
are unnamed, and the primary benchmark is built, run and hosted by the vendor with its own
product as the subject.

Proposal. Split the decision by category rather than picking one backend for all of
memory:

- A and B (in-session state, compaction): library concerns now, on PydanticAI's message
  history (`ModelMessagesTypeAdapter` round-trips exactly, so it is safe to store) plus
  compaction, with `pydantic-ai-harness` evaluated as the implementation.
- C and D (cross-session recall, user-scoped facts): ship the RFC's v1 behind the §5.3
  interface. cognee is not the right tool for three short notes: it reintroduces the three
  costs the RFC rejected, adds a graph database, and overshoots the token budget. (Amended
  by §5.5: the RFC's three-notes shape stands, its storage in learn-ai's database does not.)
- For the richer case (hundreds of memories per learner, relationships, semantic recall),
  Hindsight leads on evidence and cognee is the fallback. Hindsight needs no datastore we do
  not already run, its model calls are redirectable to our gateway by supported configuration,
  and it deletes for real. cognee's PoC gates stay on file, but its graph-database requirement
  and in-process litellm calls make it the weaker of the two unless the Hindsight PoC fails.
  Neither is adopted without a PoC that clears the items listed against it here.
- Omnigraph is not a candidate for learner memory, for the reasons above. Erasure and
  per-learner isolation are the binding constraints, and neither is an engineering gap against
  a known design. It remains the right store for what it does today, which is team-shared agent
  memory where every principal is a staff member and nothing is a learner's to delete.

### 5.5 Decisions (2026-10-01) and what stays open

Decided by the project owner:

1. The category split in §5.4 is adopted. A and B are library concerns on PydanticAI's
   message history. C and D sit behind the §5.3 interface. Hindsight's PoC runs first for the
   richer case and cognee is the fallback, tried only if Hindsight fails its PoC.
2. Memory is a shared platform service. It is not stored in, deployed with, or coupled to
   learn-ai or any other application. This answers "where memory lives": one memory service
   across apps, reached through the library's §5.3 interface, which is a client of that
   service.

What item 2 changes in this part:

- The RFC's v1 storage, a `DjangoMemoryStore` in learn-ai's database whose deletion guarantee
  is a Django foreign key, is not the supported path. The RFC's shape (a few short notes per
  learner, a gate model and an extraction model, the never-remember list) carries over; its
  store becomes the shared service, and learn-ai consumes it through `ol-llm`
  (`tk-bring-rfc-13314-s-learner-memory-onto-the-shared-853c25`).
- It favors a backend that is a service in its own right. Hindsight is one (an API over
  Postgres with per-bank isolation); `pydantic-ai-harness`'s `PostgresMemoryStore` is a
  library that would need a service or a shared database put around it.
- Tenancy has to be designed, not inherited from an app's tables. The Hindsight PoC decides
  whether a learner has one memory shared by every app or one per app, how a caller proves
  which learner it is acting for (§5.4 item 3 under Hindsight: isolation is not enforced out
  of the box), and what one app may read of what another wrote.
- Deletion gets simpler. One call to the shared service removes a learner's memory for every
  app, so account deletion in mit-learn has a single place to reach.
- Memory is keyed on the platform user id from the authenticated identity (§5.3 item 5), not
  on an app-local user row.

Still open:

1. Whether `pydantic-ai-harness` (0.x) is acceptable as the implementation for A and B, or
   whether the library implements compaction and history storage itself
   (`tk-evaluate-pydantic-ai-harness-for-in-session-stat-926e93`). The same task now has to
   say where in-session history is stored under item 2: in the shared service, or in the
   app's own database as conversation state that is not "memory". This was not asked.
2. Default retention for each category, and who executes deletion when an account is
   removed. Tied to the data-handling policy, which is on hold
   (`llm-usage-policies-spec.md` §3).
3. Whether learn-ai keeps its custom Django LangGraph checkpointer or moves to the official
   `PostgresSaver` (3.1.2), which adds `delete_thread`, `delete_for_runs`, `copy_thread` and
   `prune` at the cost of living outside Django migrations.

## 6. Call contract

The §3 decision turned into the library's API: how a call site declares itself, how the
timeout and retry values resolve, where retries and fallback happen, how output is structured,
and what the caller gets back. §6.9 holds the type stubs the implementation follows.

### 6.1 What the SDKs do by default

Measured 2026-10-01 with pydantic-ai-slim 2.52.0 against a local mock that either never
responds or returns a fixed status, calling `pydantic_ai.direct.model_request` with
`ModelSettings(timeout=1)` and default provider construction.

| Provider | HTTP attempts on a hang, 429, or 503 | A hang surfaces as | On a 400 |
| --- | --- | --- | --- |
| OpenAI (openai 3.22.1) | 3 | `ModelAPIError` from `openai.APITimeoutError` | 1 attempt, `ModelHTTPError` |
| Anthropic (anthropic 1.11.0) | 3 | `ModelAPIError` from `anthropic.APITimeoutError` | 1 attempt, `ModelHTTPError` |
| Bedrock (boto3 1.43.107) | 5 | `ModelAPIError` from `botocore.exceptions.ReadTimeoutError`, after 12.7 s for a 1 s read timeout | 1 attempt, `ModelHTTPError` |
| Google (google-genai 2.26.0) | 1 | raw `httpx2.ReadTimeout` | 1 attempt, `ModelHTTPError` |
| Mistral (mistralai 3.0.0) | 1 | raw `httpx2.ReadTimeout` | 1 attempt, `ModelHTTPError` |

Three consequences for the contract:

- Left alone, a 300 s batch timeout on Bedrock is five attempts and more than 25 minutes
  before the caller sees an error, and any retry the library adds multiplies that.
- `FallbackModel`'s default `fallback_on=(ModelAPIError,)` does not catch a Google or Mistral
  timeout, so those would skip fallback entirely.
- Bedrock still ignores `ModelSettings.timeout` in 2.52.0 (`models/bedrock.py` never reads
  it). Its timeout comes only from the boto3 client's `read_timeout` and `connect_timeout`.

429 and 5xx responses map to `ModelHTTPError` with `status_code` and a parsed `retry_after` on
all five providers.

### 6.2 Declaring a use case and resolving its policy

Every call site names a `UseCase`, declared once as a module-level constant. It carries the
§4 attribution fields the call site owns (`app`, `name`), the profile (`request` or `batch`),
the model, and any policy defaults that differ from the profile's.

The three policy settings resolve in the §3.2 order, later wins:

1. the profile default (request: 60 s, 2 retries, 10 s; batch: 300 s, 5 retries, 300 s);
2. a value set on the `UseCase`, which requires `policy_reason`; building a model from a use
   case that overrides a setting without a reason raises `ValueError`;
3. `OL_LLM_<PROFILE>_<SETTING>`, with `<PROFILE>` one of `REQUEST` or `BATCH`;
4. `OL_LLM_<USE_CASE>_<SETTING>`, with the use case name upper-cased and every character
   outside `A-Z0-9` replaced by `_` (`course-translation` reads
   `OL_LLM_COURSE_TRANSLATION_TIMEOUT_SECONDS`).

`<SETTING>` is `TIMEOUT_SECONDS`, `MAX_RETRIES`, or `MAX_BACKOFF_SECONDS`. A use case may not
be named `request` or `batch`. An environment value that does not parse raises `ValueError`
when the model is built, not on first use. The resolved values go on the root trace as
`opik.metadata.policy_timeout_seconds`, `policy_max_retries`, and `policy_max_backoff_seconds`.

What the numbers mean:

- `TIMEOUT_SECONDS` bounds one attempt. On a streamed response it bounds the gap between
  chunks, not the whole stream: a mock stream of six chunks 0.6 s apart completed in 3.6 s
  under `timeout=1`, and a stall after two chunks raised `ModelAPIError` 1 s later (measured
  on the OpenAI model class only).
- Worst-case wall clock for one call is `(MAX_RETRIES + 1) × TIMEOUT_SECONDS` plus backoff:
  190 s on the request profile and 3,300 s on the batch profile at the defaults, and that
  again for each fallback model. Celery time limits, the arq `job_timeout`, and Dagster op
  timeouts have to sit above it. The course-translation Celery limit is 29 minutes soft
  (`settings/common.py:62-67`), which is below one batch-profile call's worst case, so that
  use case needs a lower retry count or timeout when it adopts.

### 6.3 Model construction and the single retry layer

`build_model(use_case)` returns a PydanticAI `Model`. It is the only supported way to get one,
and it does three things.

It turns every SDK's own retries off. For Bedrock that means passing its own boto3 client,
which is also where the timeout goes; `BedrockProvider(aws_read_timeout=,
aws_connect_timeout=)` in §3.2 sets the same two botocore values but cannot disable
retries:

| Provider | Timeout applied as | SDK retries disabled by |
| --- | --- | --- |
| OpenAI, Azure OpenAI | `ModelSettings.timeout` on each request | `AsyncOpenAI(max_retries=0)` or `AsyncAzureOpenAI(max_retries=0)`, passed as `openai_client` |
| Anthropic | `ModelSettings.timeout` | `AsyncAnthropic(max_retries=0)`, passed as `anthropic_client` |
| Bedrock | botocore `Config(read_timeout=, connect_timeout=)`, both from the policy timeout | `Config(retries={"total_max_attempts": 1})` on a client passed as `bedrock_client` |
| Google (Gemini API and Vertex) | `ModelSettings.timeout` (a float; `httpx.Timeout` raises `UserError`) | nothing to disable, one attempt by default |
| Mistral | `ModelSettings.timeout` | nothing to disable, one attempt by default |

It wraps the provider model in a `WrapperModel` subclass that owns the retry loop. The wrapper
sets `timeout` on every request from the resolved policy, overriding whatever the caller
passed, so a model the library built cannot make a request without one. It classifies failures
by type and status:

| Raised underneath | Class | Library error | Retry | Fall back |
| --- | --- | --- | --- | --- |
| `ModelHTTPError` 429 | rate limited | `LLMRateLimited` (carries `retry_after`) | yes | yes |
| `ModelHTTPError` 408, 500, 502, 503, 504, 529 | provider unavailable | `LLMUnavailable` | yes | yes |
| `ModelAPIError` that is not `ModelHTTPError`; `TimeoutException`, `NetworkError`, and `RemoteProtocolError` from `httpx` or `httpx2` | provider unavailable | `LLMUnavailable` | yes | yes |
| `ModelHTTPError`, any other status; any other `httpx` or `httpx2` `TransportError` | rejected | `LLMRejected` (carries `status_code`, `None` for a transport error) | no | no |

The retry rows are the §3.2 statuses and the transport errors that mean no answer came back.
Everything else is rejected, which closes the table so nothing is unclassified. A permanent
failure (a 501 or 505, `UnsupportedProtocol` from a bad base URL, `LocalProtocolError`,
`ProxyError`) then costs one attempt, not the full retry budget on every model in the
chain, and opens the batch circuit at once (§6.7).

The split by transport error type reaches only Google and Mistral, which raise raw `httpx2`
exceptions (§6.1). The OpenAI and Anthropic SDKs wrap every transport failure in
`APIConnectionError` (`openai/_base_client.py:1183`, `anthropic/_base_client.py:1317`) and
PydanticAI turns that into `ModelAPIError` (`models/openai.py:243-244`,
`models/anthropic.py:410-411`), so on those a bad URL scheme is retried like a dropped
connection. The wrapper does not inspect `__cause__` to tell them apart.

Backoff is exponential with jitter, starting at 0.5 s. On the request profile
`MAX_BACKOFF_SECONDS` is a total across the call, and a `Retry-After` longer than what is left
of it ends the call at once with `LLMRateLimited`, so the caller can tell the user or
reschedule instead of holding a request open. On the batch profile it caps each wait, and a
longer `Retry-After` waits the cap and tries again.

Because the wrapper sits at the `Model` level it covers Bedrock, which an HTTP-transport
retry (`pydantic_ai.retries`) cannot, and it applies equally to agents and to
`pydantic_ai.direct`.

A scratch implementation of this wrapper, run against the same mock, made exactly three
attempts on a hang, a 429, and a 503 for all five providers, one attempt on a 400, and raised
the library error with the PydanticAI exception as `__cause__`. Streams retried while opening
and not after. The library keeps that mock as a test, one case per provider, because the
attempt count depends on SDK defaults that can change in any release.

### 6.4 Fallback

A use case with `fallback_models` gets
`FallbackModel(wrapped_primary, *wrapped_fallbacks, fallback_on=(LLMRateLimited, LLMUnavailable))`.
Each model in the chain is wrapped separately and runs its own retries first, so fallback
starts only after the primary's retry budget is spent. Measured on the scratch wrapper: a 503
from both models made six attempts and raised `FallbackExceptionGroup`, and a 400 from the
primary made one attempt and raised `LLMRejected` without trying the fallback.

- When every model fails, the entry points raise the primary model's error, with the
  `FallbackExceptionGroup` as `__cause__`.
- When a fallback model answers, the result is marked degraded with reason `fallback_model`
  (§6.7), and `LLMResult.model` names the model that answered.
- Streams fall back only while opening (`models/fallback.py:325-337` in 2.52.0). A failure
  after the first chunk reaches the caller as `LLMUnavailable`.
- A model whose gateway route already falls back takes no `fallback_models`. The model catalog
  (`tk-policy-model-catalog-and-lifecycle-ba2fc7`) records which routes do, and
  `build_model` raises `ValueError` for a use case that sets both.

### 6.5 Structured output

- A call that needs structure declares an `output_type`. Parsing model text with regular
  expressions or sentinel markers in application code is not a supported pattern.
- `ToolOutput` is the default. It is PydanticAI's own default mode
  (`default_structured_output_mode` is `tool`) for the OpenAI, Anthropic, Google, Mistral, and
  Bedrock models checked.
- `NativeOutput` is used where the use case asks for it and the model profile has
  `supports_json_schema_output` set. It is unset for `mistral-large-latest`, and asking for it
  there raises `ValueError` at build time.
- `PromptedOutput` is allowed only when the use case passes it explicitly, for a model with
  neither tools nor native output.
- Free text is `output_type=str`.
- Validation failures retry on the same model (§3.2 rule 3). `UseCase.output_retries` sets
  `Agent(retries={"output": n})`, default 1, which is PydanticAI's default. When they are
  spent the caller gets `LLMInvalidOutput`.

### 6.6 Entry points

Both paths share the model factory, policy, attribution, tracing, and errors.

- Agent path: `build_agent(use_case, ...)` returns a PydanticAI `Agent` over the wrapped
  model. `run`, `run_sync`, and `run_stream` execute it and return `LLMResult`.
- Single-call path: `complete` and `complete_sync` take a use case and a prompt or message
  list and return `LLMResult`, with no agent for the caller to build. Structured output and
  its validation retries behave as in §6.5. Whether the implementation uses
  `pydantic_ai.direct` or a tool-less agent internally is not part of the contract;
  `direct.model_request` has no output validation, so structured output needs the latter.
- The `_sync` variants exist for Celery tasks and Dagster assets.
- Streaming is for the request profile. `run_stream` is an async context manager yielding an
  object with `stream_text()`, `stream_output()`, and `result()`; `result()` is available
  once the stream is consumed and carries the same markers as a non-streamed call.
- Call sites that are not built on PydanticAI (learn-ai's LangGraph bots) take the numbers
  from `resolve_policy(use_case)` and apply them to their own client. The retry wrapper and
  typed errors apply only to models the library builds.

### 6.7 Errors and degraded results

The entry points raise only `LLMError` subclasses for model failures, each with the original
exception as `__cause__` and with `use_case`, `model`, and `retryable` set.

| Library error | From |
| --- | --- |
| `LLMRateLimited`, `LLMUnavailable`, `LLMRejected` | the retry wrapper (§6.3) |
| `LLMContentFiltered` | `ContentFilterError` |
| `LLMInvalidOutput` | `UnexpectedModelBehavior` once output retries are spent, including a response cut off at the token limit |
| `LLMBudgetExceeded` | `UsageLimitExceeded` |

`ContentFilterError` subclasses `UnexpectedModelBehavior`, so it is matched first. By default
PydanticAI raises it only when a filtered response has no usable content
(`_agent_graph.py:2212-2225`); agents the library builds carry the `RaiseContentFilterError`
capability so a filtered response with partial text is an error too.

Request-time surfaces map the error class to their own user-facing text. Provider messages
stay on the exception and the trace and are never shown to users.

`LLMResult.degraded` is true, with one or more reasons, when the output came from a fallback
model (`fallback_model`), was rendered from the embedded prompt default because Opik was
unreachable (`embedded_prompt`), or is knowingly incomplete (`partial_output`, e.g.
translation segments left in the source language). The same values go on the trace as
`opik.metadata.degraded` and `opik.metadata.degraded_reason` (§4.1).

The library sets the first two. `partial_output` comes from the call site, through the
`postprocess` argument of `run`, `run_stream`, and `complete`: a callable that takes the
validated output and returns a `PostProcessed` holding the output to return and any degraded
reasons. It runs inside the root trace (for a stream, in `result()`), so the result and the
trace are marked together. `LLMResult` is frozen and the trace is closed once the entry point
returns, so a check done after the call cannot mark either. `postprocess` keeps the output
type, and an exception it raises reaches the caller unchanged.

For the batch helpers (§3.2 rule 7): `LLMRejected` opens the circuit at once,
`LLMRateLimited` and `LLMUnavailable` count toward its threshold, and `LLMInvalidOutput`,
`LLMContentFiltered`, and `LLMBudgetExceeded` are failures of that row only.

### 6.8 Usage limits

Every run gets a `UsageLimits`. `request_limit` keeps PydanticAI's default of 50 model
requests per run unless the use case sets it. `cost_limit` is a per-run dollar ceiling. It stays
unset in phase one: the budgets decision (2026-10-01, `llm-usage-policies-spec.md` §1) is
alerts only until the spend baseline exists. PydanticAI prices a run from `genai-prices` and only warns
(`CostNotFoundWarning`) when it has no price for the model, so a `cost_limit` on an unpriced
model enforces nothing. The library's test suite checks that every catalog model has a price.

### 6.9 Type stubs

`ol_llm/__init__.pyi`. Checked with `mypy --strict` against pydantic-ai-slim 2.52.0 together
with a sample batch caller and a sample streaming caller.

```python
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Generic, Literal, TypeVar, overload

from pydantic_ai import Agent
from pydantic_ai.messages import ModelMessage
from pydantic_ai.models import Model
from pydantic_ai.output import OutputSpec
from pydantic_ai.usage import RunUsage

OutputT = TypeVar("OutputT")
DepsT = TypeVar("DepsT")

Profile = Literal["request", "batch"]
DegradedReason = Literal["fallback_model", "embedded_prompt", "partial_output"]


@dataclass(frozen=True, kw_only=True)
class CallPolicy:
    timeout_seconds: float
    max_retries: int
    max_backoff_seconds: float


@dataclass(frozen=True, kw_only=True)
class UseCase:
    app: str
    name: str
    profile: Profile
    model: str
    fallback_models: Sequence[str] = ()
    timeout_seconds: float | None = None
    max_retries: int | None = None
    max_backoff_seconds: float | None = None
    policy_reason: str | None = None
    output_retries: int = 1
    request_limit: int | None = 50
    cost_limit: Decimal | None = None


@dataclass(frozen=True, kw_only=True)
class Attribution:
    user_id: str | None = None
    thread_id: str | None = None
    run_id: str | None = None


@dataclass(frozen=True, kw_only=True)
class PostProcessed(Generic[OutputT]):
    output: OutputT
    degraded_reasons: tuple[DegradedReason, ...] = ()


@dataclass(frozen=True, kw_only=True)
class LLMResult(Generic[OutputT]):
    output: OutputT
    model: str
    usage: RunUsage
    degraded: bool
    degraded_reasons: tuple[DegradedReason, ...]
    prompt_name: str | None
    prompt_version: str | None
    new_messages: Sequence[ModelMessage]


class LLMError(Exception):
    use_case: str
    model: str
    retryable: bool


class LLMRateLimited(LLMError):
    retry_after: float | None


class LLMUnavailable(LLMError): ...


class LLMRejected(LLMError):
    status_code: int | None


class LLMContentFiltered(LLMError): ...


class LLMInvalidOutput(LLMError): ...


class LLMBudgetExceeded(LLMError): ...


def resolve_policy(use_case: UseCase) -> CallPolicy: ...
def build_model(use_case: UseCase) -> Model: ...
@overload
def build_agent(
    use_case: UseCase,
    *,
    output_type: OutputSpec[OutputT] = ...,
    instructions: str | None = None,
    tools: Sequence[Any] = (),
    toolsets: Sequence[Any] | None = None,
    capabilities: Sequence[Any] | None = None,
) -> Agent[None, OutputT]: ...
@overload
def build_agent(
    use_case: UseCase,
    *,
    deps_type: type[DepsT],
    output_type: OutputSpec[OutputT] = ...,
    instructions: str | None = None,
    tools: Sequence[Any] = (),
    toolsets: Sequence[Any] | None = None,
    capabilities: Sequence[Any] | None = None,
) -> Agent[DepsT, OutputT]: ...


async def run(
    agent: Agent[DepsT, OutputT],
    user_prompt: str | None = None,
    *,
    deps: DepsT = ...,
    attribution: Attribution = ...,
    message_history: Sequence[ModelMessage] | None = None,
    postprocess: Callable[[OutputT], PostProcessed[OutputT]] | None = None,
) -> LLMResult[OutputT]: ...


# run_sync has the signature of run without `async`.


class LLMStream(Generic[OutputT]):
    def stream_text(self, *, delta: bool = True) -> AsyncIterator[str]: ...
    def stream_output(self) -> AsyncIterator[OutputT]: ...
    async def result(self) -> LLMResult[OutputT]: ...


def run_stream(
    agent: Agent[DepsT, OutputT],
    user_prompt: str | None = None,
    *,
    deps: DepsT = ...,
    attribution: Attribution = ...,
    message_history: Sequence[ModelMessage] | None = None,
    postprocess: Callable[[OutputT], PostProcessed[OutputT]] | None = None,
) -> AbstractAsyncContextManager[LLMStream[OutputT]]: ...


async def complete(
    use_case: UseCase,
    messages: Sequence[ModelMessage] | str,
    *,
    output_type: OutputSpec[OutputT] = ...,
    instructions: str | None = None,
    attribution: Attribution = ...,
    postprocess: Callable[[OutputT], PostProcessed[OutputT]] | None = None,
) -> LLMResult[OutputT]: ...


# complete_sync has the signature of complete without `async`.
```

`Attribution` holds the per-call §4 fields. `app` and `use_case` come from the `UseCase`,
`environment` from deployment configuration, `model` and `provider` from the model that
answered, and the prompt fields from the prompt helper, whose signature belongs to
`tk-decide-prompt-overrides-cache-behavior-fallback--5e7c55`.

### 6.10 Not verified

- Azure OpenAI was not probed separately. It uses the same OpenAI SDK client, so the
  `max_retries=0` setting is expected to behave as measured for OpenAI.
- Vertex was probed through `GoogleProvider` with an API key, not through
  `GoogleCloudProvider`.
- The stream timeout semantics were measured on the OpenAI model class only.
- The stubs type-check; nothing behind `build_agent`, `run`, or `complete` exists yet. The
  retry wrapper is the only part with a working scratch implementation.
- The scratch wrapper was run against a hang, a 429, a 503, and a 400. The rejected rows for
  other 5xx statuses and for non-retryable transport errors (§6.3) were not run.

## 7. Durable execution for agent workflows

Added 2026-10-01 at the project owner's direction: the platform should look toward supporting
persistent workflows for agentic use cases, through DBOS or Temporal. Nothing here is
decided. This part records the starting facts and what the evaluation has to answer.

### 7.1 What exists

- No integration can resume an agent run that died partway. learn-ai's LangGraph checkpointer
  persists conversation state between turns, not a run in flight. Batch pipelines get their
  resumability from the orchestrator: Dagster checkpoints in ol-data-platform, Celery retries
  in mit-learn and the translation plugins.
- Neither engine is deployed as a platform service. The only Temporal in ol-infrastructure is
  the one Airbyte bundles for itself (`applications/airbyte/`). DBOS appears nowhere.
- PydanticAI 2.52.0 ships wrappers for both: `pydantic_ai.durable_exec.temporal.TemporalAgent`
  (extra `temporal`, `temporalio>=1.27.0`) and `pydantic_ai.durable_exec.dbos.DBOSAgent` (extra
  `dbos`, `dbos>=2.10.0`). Each wraps an ordinary `Agent` and runs model requests and tool
  calls as the engine's units: Temporal activities, configured by `activity_config` and
  `model_activity_config` with a `retry_policy` and a default `start_to_close_timeout` of
  60 s; DBOS steps, configured by `model_step_config` with `retries_allowed` and
  `max_attempts`. A Prefect wrapper exists too.

### 7.2 What it means for the rest of this spec

- A third retry layer appears. The engine retries a failed activity or step on its own, on
  top of the library's wrapper (§6.3) and under any fallback chain. The one-retry-layer rule
  has to be restated for durable runs: either the engine's retries are off for model steps
  and the wrapper retries inside the step, or the reverse.
- Temporal's default activity timeout (60 s) is the request profile's timeout and far below
  the batch profile's 300 s, so the activity config has to be derived from the resolved
  `CallPolicy`.
- `TemporalAgent` needs model instances registered up front (`models=`), and inside a
  workflow only registered instances or names are valid. `build_model`'s wrapped models have
  to be registrable that way.
- `build_model` returning a plain `Model` and `build_agent` a plain `Agent` (§6.9) is what
  lets either wrapper be applied. The library core keeps that shape, so durable execution
  can be added without changing call sites that do not need it.
- The workflow id is the natural `run_id` in the attribution schema (§4.1), and a resumed run
  has to land in the same Opik trace or thread as the run it continues. Whether the
  `@opik.track` root trace survives a resume in another process is not known.

### 7.3 What the evaluation has to answer

1. Which use cases need it. No current integration is a long-running agent; the candidates
   are future ones (multi-step tutoring or authoring agents, anything that waits on a person).
2. Operating cost. Temporal is a server to run: several services and a database of its own.
   DBOS is a library that checkpoints into Postgres, which every app already has. Both
   statements are from the projects' own descriptions and were not verified here.
3. Fit with the runtimes we have: Django with Celery, Dagster, FastAPI with arq.
4. Whether a run survives a pod restart mid-tool-call and resumes without repeating a
   completed model request, with the §3 error contract intact.
5. Waiting on a human (approval, a learner's reply) for hours or days.
6. Tracing continuity in Opik across a resume.
7. Whether the engine, if it is a service, is shared across apps the way memory is (§5.5).

Out of scope until that evaluation reports: adding `temporal` or `dbos` extras to `ol-llm`.
