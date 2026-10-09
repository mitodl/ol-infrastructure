# Spec: LLM usage policies, adoption order, and ownership

Tracked as workflow project `wp-platform-supported-llm-capabilities-ecca0b`. Each part
answers one decision task. Decisions were made by the project owner on 2026-10-01.

| Part | Decision task | Status |
| --- | --- | --- |
| §1 Budgets and rate limits | `tk-policy-budgets-and-rate-limits-for-llm-usage-3284fe` | Decided: alerts only in phase one |
| §2 Model catalog and lifecycle | `tk-policy-model-catalog-and-lifecycle-ba2fc7` | Decided: one catalog in `ol-llm`, floating aliases allowed |
| §3 Data handling and retention | `tk-policy-data-handling-and-retention-for-prompts-c-76830f` | On hold; no proposal in this document |
| §4 Embeddings and MCP scope | `tk-policy-scope-of-embeddings-and-mcp-tool-access-i-3d8034` | Decided as proposed |
| §5 Adoption order | `tk-decide-the-first-adopter-and-whether-existing-in-54a13c` | Decided as proposed |
| §6 Ownership and success criteria | `tk-decide-ownership-and-success-criteria-for-the-pl-8445bd` | Decided, no target date |

The error contract, the fifth policy in this epic, was decided on 2026-09-11 and is §3 of
`shared-llm-library-spec.md`.

Source read on 2026-10-01: learn-ai `9f29bda`, mit-learn `b6d0e97`, open-edx-plugins
`50483d3`, ol-data-platform `f6d38b2`, gwarek `ee8eaae`, ol-infrastructure `03ffe4f`,
apache/apisix 3.18.0, comet-ml/opik 2.2.81 and `3de1c5e`, pydantic-ai-slim 2.52.0.
Infrastructure paths are under `src/ol_infrastructure/applications/`.

## 1. Budgets and rate limits

### 1.1 What exists

- Nothing measures or caps dollars. There is no Grafana dashboard or alert rule for tokens or
  cost.
- learn-ai throttles requests per user per day from database rows: 1000 authenticated and 500
  anonymous for most bots (`main/models.py:59-87`, seeds in `main/migrations/0001` to
  `0003`), with staff exempt. Its per-customer token, request, and dollar limits
  (`ai_chatbots/proxies.py:105-112`) apply only behind a LiteLLM proxy that is not deployed.
- mit-learn rate-limits embedding tasks through Celery (`vector_search/tasks.py:160-166`).
  ol-data-platform bounds concurrency in code (`SUMMARIZE_MAX_CONCURRENCY=20`,
  `EMBEDDING_MAX_CONCURRENCY=20`); each `ml` asset names a Dagster pool, and no limit
  value for those pools was found in either repository.
- Neither gateway candidate enforces dollars. Both enforce token quotas per window and both
  charge a request after it completes, so the request that crosses the limit is served.
  APISIX rejects with 503 unless configured otherwise (`ai-rate-limiting.lua:88-90`).
- PydanticAI enforces a per-run `cost_limit` from its own price data and only warns when it
  has no price for the model (`usage.py:569-589`).

### 1.2 Decision

Decided 2026-10-01: phase one is alerts only. Nothing new is enforced until the spend
baseline (`tk-measure-the-llm-spend-baseline-before-any-migrat-bb6682`) exists.

In phase one:

- The library emits token counters on every path (labels `app`, `use_case`, `provider`,
  `model`, `environment`). Dollars are computed in Prometheus from those counters and a
  price table kept with the model catalog (§2), joined on `provider` and `model` because
  catalog ids are `provider:model` and the same model name can carry a different price from
  another provider (OpenAI direct vs. Azure OpenAI). Grafana alerts go to Rootly at 50, 80,
  and 100 percent of a monthly figure per app once the baseline supplies one.
- Vendor invoices are the accounting record and are reconciled monthly. Opik's per-trace cost
  is a debugging aid, not a ledger.
- What already bounds usage stays: learn-ai's per-user request throttles, Celery rate limits,
  orchestrator concurrency, and PydanticAI's default of 50 model requests per run, which is a
  guard against a looping agent and not a budget.
- `cost_limit` stays unset and no gateway quota is required.

### 1.3 Proposed for when enforcement is turned on

Not decided. The recommendation was to enforce from the start; this is what it would look
like.

| Control | Unit | Enforced by | On exhaustion |
| --- | --- | --- | --- |
| Per-application quota | tokens per day | a gateway route, where a team has opted into one | reject with 429; the library raises `LLMRateLimited` |
| Per-run ceiling | dollars (`cost_limit`) and model requests (`request_limit`) | the library, on every path | `LLMBudgetExceeded` |
| Per-end-user limit | requests per day | the application, as learn-ai does today | the app's own message |
| Batch throughput | concurrency | the orchestrator (Dagster pools, Celery rate limits) | queueing |
| Monthly spend per app and use case | dollars | not enforced: Grafana alerts to Rootly at 50, 80, and 100 percent | a person decides |

- Enforcement is in tokens and requests. Dollars are for reporting and alerts.
- Exhaustion fails the call. There is no automatic switch to a cheaper model: fallback exists
  for availability (§3 of the library spec), and using it for budget would change output
  quality without anyone choosing that.
- Gateway quota rejections use 429. A 503 would be classified as provider-unavailable,
  retried, and sent to the fallback model.
- Per-end-user token limits at the gateway are deferred. APISIX can key a limit on a request
  variable, but it has no Redis today, and learn-ai's request throttles already bound a
  single user.
- Budgets per app and the default `cost_limit` per profile come from the spend baseline.

## 2. Model catalog and lifecycle

### 2.1 What exists

Model ids in deployed configuration:

| Model | Used by | Set in |
| --- | --- | --- |
| OpenAI `gpt-4o-mini` | learn-ai recommendation, syllabus, and video bots; mit-learn credential metadata | `learn_ai/Pulumi.Production.yaml:13-16`; mit-learn migration `0124` |
| OpenAI `gpt-4o` | learn-ai tutor; mit-learn Canvas PDF transcription | `learn_ai/Pulumi.Production.yaml:13-16`; `mit_learn/__main__.py:1525` |
| OpenAI `gpt-5-nano-2025-08-07` | mit-learn OCR | `mit_learn/__main__.py:1583` |
| OpenAI `gpt-5.2` | course translations | `edxapp/k8s_secrets.py:395-409` |
| OpenAI `text-embedding-3-large` | mit-learn dense embeddings | `mit_learn/__main__.py:1573` |
| Gemini `gemini-3-pro-preview` | course translations | `edxapp/k8s_secrets.py:395-409` |
| Mistral `mistral-large-latest` | course translations (default provider) | `edxapp/k8s_secrets.py:395-409` |
| Bedrock `global.anthropic.claude-haiku-4-5-20251001-v1:0` | Dagster feedback summaries, categories, sentiment | code default, `ml/lib/summarize.py:75-78` |
| Bedrock `amazon.titan-embed-text-v2:0` | Dagster feedback embeddings | code default, `ml/lib/embed.py:62-64` |
| Bedrock `us.anthropic.claude-sonnet-5` | gwarek | `gwarek/__main__.py:635-637` |
| Azure OpenAI deployments `gpt-4o`, `gpt-5-mini`, `gpt-5.2`, and embeddings | provisioned; enabled for learn-ai and mit-learn in CI only | `infrastructure/azure/openai/__main__.py:124,134` |

mit-learn's summary and flashcard model is a per-platform database row
(`ContentSummarizerConfiguration.llm_model`) and its value was not read.

The lists that say what is allowed are per app and disagree with what runs. learn-ai's
`LLMModel` table is admin-editable, publicly listable, and its seed still enables
`gpt-4-turbo`, `o1-mini`, `o3-mini`, and `claude-3-5-sonnet-20241022`
(`fixtures/migrations/0012_llm_model_temp_reasoning.json`). The translation defaults are
duplicated in two plugins' settings and in infrastructure. ol-data-platform maps model names
to an Opik pricing provider by hand (`ml/resources/opik_auth.py:206-213`).

### 2.2 Decision

Decided 2026-10-01: items 1 to 7 below.

1. One catalog, a data file in the `ol-llm` repository, released with the library. Each entry
   has the id in PydanticAI form (`provider:model`), the route (a gateway route or direct,
   and whether that route falls back on its own), a status (`allowed`, `deprecated` with a
   retirement date, `retired`), whether the id is pinned or a floating alias, the name Opik
   prices it under, and input and output prices.
2. `build_model` accepts only catalog ids. A deprecated id works and tags the trace; a retired
   id raises at build time. App-side lists (learn-ai's `LLMModel` rows, mit-learn's
   configuration rows) hold catalog ids and are checked against the catalog in CI.
3. Adding or changing an entry is a pull request to `ol-llm`, approved by the catalog owner
   (§6). It needs: the provider covered by the data-handling policy (§3) once that exists, a price in
   `genai-prices` and in Opik or added to both, and for a use case switching to it, an
   evaluation run (lifecycle spec §5; while that use case's gate is advisory the run informs
   the review and does not block it).
4. Floating aliases (`mistral-large-latest`) and preview models (`gemini-3-pro-preview`) are
   allowed for any use case. The catalog records that an id floats, because such a model can
   change behavior with no catalog change and no evaluation. Restricting them to
   advisory-gated use cases was the recommendation and was not adopted.
5. Bedrock entries use inference-profile ids, since that is what the IAM policies grant and
   what the callers send.
6. Deprecation reaches apps as a library release through Renovate. Retirement is a release
   that fails CI for any app still naming the id.
7. The initial catalog is the §2.1 table. The stale learn-ai seed rows are not carried over.

The alternatives considered were the gateway's route configuration (it cannot describe traffic that
bypasses the gateway), Opik (it has no such object), and a catalog service (a new thing to
run for a list that changes a few times a year).


## 3. Data handling and retention

On hold by the project owner's decision on 2026-10-01. No policy is adopted and none is
proposed in this document. The decision waits on answers from outside engineering: which
providers are approved for learner content, how long applications may keep learner
conversations, and whether edX tracking logs are in scope. The working proposal and the
inventory behind it are on the decision task and return here once those are answered.

Two facts other parts of these specs depend on, both from Opik source at 2.2.81:

- Opik's trace retention job is off by default (`RETENTION_ENABLED`). When on, periods are a
  fixed set of 14, 60, or 400 days or unlimited, set by rules scoped to organization,
  workspace, or project (`RetentionPeriod.java:15-18`, `RetentionLevel.java:15-17`).
- Opik captures trace inputs and outputs by default, and PydanticAI's spans carry message
  content unless `InstrumentationSettings(include_content=False)`. Whether capture is
  switched off for any use case is part of this policy.

## 4. Embeddings and MCP scope

### 4.1 Embeddings

Two integrations embed. mit-learn's `LiteLLMEncoder` calls `litellm.embedding` with a Redis
cache on query paths, hedged requests, bounded pools, and a 10 s timeout
(`vector_search/encoders/litellm.py:129-267`). ol-data-platform embeds through raw SDKs with
per-provider batch sizes and row-by-row fallback for isolatable errors
(`ml/lib/embed.py:74-516`), and keys stored vectors on model and dimension so a model change
re-embeds everything and leaves the old vectors in place (`embed.py:25-27, 611`).

Decided 2026-10-01:

- Embedding calls are LLM call sites and come under the library: catalog ids, the timeout and
  retry policy, attribution, tracing, and token metrics. PydanticAI 2.52 has an `Embedder`
  with OpenAI, Bedrock, and Google backends and a wrapper class, so the same retry wrapper
  applies.
- A gateway route is optional for them, as for chat.
- What stays in the application: hedging, caching, batching to provider limits, and the
  vector store. Those are tuned to each workload.
- Changing an embedding model is a catalog lifecycle event with a re-embedding plan attached.
  Vectors are stored with the model id and dimension that produced them.
- The library's embedding entry point comes after the chat core. Until then mit-learn and
  ol-data-platform keep their current embedding code.

### 4.2 MCP and tool traffic

No production application has a model call MCP tools. agent-kit serves MCP tools to coding
agents and makes no model calls. APISIX has `mcp-bridge` enabled and 3.19.0 adds
`openapi-to-mcp`; Agent Router has an MCP gateway.

Decided 2026-10-01: governing tool traffic is out of phase one and does not weigh in the gateway
choice. Tools used inside an agent built on the library are traced as tool spans like the
rest of the run. This is revisited when the first production agent needs a remote tool
server.


## 5. Adoption order

"Every call site goes through the shared library" is settled, so all five integrations
migrate. Decided 2026-10-01: this order.

| Order | Integration | Why here |
| --- | --- | --- |
| 1 | ol-data-platform feedback pipeline | Already the closest to the target: it uses the Opik SDK for tracing and the Prompt Library, records the prompt version per row, and has checkpointing and a circuit breaker (`ml/resources/opik_auth.py`, `ml/lib/summarize.py`). It exercises the batch profile, has no learner-facing surface, and is owned by the team building the library, so the API gets its first real use where iteration is cheapest |
| 2 | mit-learn credential metadata | Smallest request-time path: one structured-output call with a documented timeout and an audit log (`learning_resources/credentials.py`). It is the natural first caller if a gateway route is offered |
| 3 | gwarek | One forced tool call on Bedrock. Small, and proves the Anthropic-on-Bedrock path |
| 4 | learn-ai | Opik wiring, prompts, and LangSmith retirement through the library while LangGraph stays. It is the most exposed integration and depends on the prompt helper and the CI prompt sync |
| 5 | mit-learn summaries, flashcards, OCR | Batch on Celery; needs the batch helpers |
| 6 | Course-translation plugins | Most to gain and hardest to package: installable on edx-platform master only, and the Celery time limit is below the batch profile's worst case (call contract §6.2) |

New LLM work uses the library from the day its core ships and does not wait for this order.

Two pieces of the learn-ai work do not need the library and can start now: removing the
LangSmith trace wrapper (`ai_chatbots/consumers.py:353-370`) and dropping the dead
dependencies. Replacing the LangSmith prompt loader does need the prompt helper.


## 6. Ownership and success criteria

Decided 2026-10-01. Teams are named; individuals are not.

| Component | Owner |
| --- | --- |
| LLM gateway, if one is offered | Platform Engineering |
| Production Opik and the ClickHouse behind it | Platform Engineering |
| `ol-llm` library and the model catalog | Platform Engineering, with app-team reviewers |
| Prompt and evaluation lifecycle (the CI jobs, the runbook) | Platform Engineering |
| Each use case: its declaration, prompts, datasets, thresholds, and budget | the application team that ships it |
| Data-handling approvals (§3) | to be named |

Criteria for done:

1. No model call outside `ol-llm` in the five integrations, checked by an import rule in each
   repository's CI. Two scoped exceptions follow from decisions already made: learn-ai's
   LangGraph bots, which keep their client and take their policy values, prompts, and Opik
   wiring from the library until LangGraph is replaced, and embedding calls until the
   library's embedding entry point exists (§4.1).
2. Every model call in the last 7 days has a trace in the shared Opik with `app`, `use_case`,
   `environment`, and `model` set.
3. Spend is reported per application and use case, and the monthly total is within 10 percent
   of vendor invoices.
4. Every production prompt is in the Prompt Library, and a prompt change reaches production
   without an application deploy.
5. Every learner-facing use case has an evaluation gate, advisory or blocking.
6. LangSmith is gone from code, configuration, and Vault.

A seventh proposed criterion, about provider keys leaving applications, was dropped with the
decision that the gateway is optional.

Sequencing: the library core, Opik hardening, and the gateway PoC run in parallel. Adoption
waits on the library core. The CI prompt sync waits on the access model and on apps being
wired to the shared instance. A policy is decided before the component that encodes it is
built.

There is no target date.
