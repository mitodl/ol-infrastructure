# Spec: LLM gateway coverage rule, requirements, shortlist, and PoC scoring

Tracked as workflow project `wp-platform-supported-llm-capabilities-ecca0b`, gateway epic
`tk-llm-gateway-selection-619732`.

| Part | Task | Status |
| --- | --- | --- |
| §2 Coverage rule | `tk-decide-which-model-traffic-must-traverse-the-gat-e53aaa` | Decided 2026-10-01: the gateway is optional in phase one |
| §3 to §6 Requirements, shortlist, acceptance, scoring | `tk-write-gateway-requirements-candidate-shortlist-a-f6cf53` | Written 2026-10-01 |

Settled inputs (2026-09-10): evaluate APISIX first and stay open to others; avoid the LiteLLM
proxy's performance profile; static provider keys in Vault are acceptable and federated
credentials are preferred where possible; every LLM call site goes through the shared library
(`shared-llm-library-spec.md`), which owns Opik tracing, attribution, timeouts, and retries.

Source read on 2026-10-01: apache/apisix tag 3.18.0 (`0796d9c`) and master (`c6b2adc`),
theagentrouter/agent-router tag v1.1.0 (`c217da8`) and main (`97f5609`), ol-infrastructure
`03ffe4f`. Nothing below was run against a gateway; that is what the PoCs are for.

## 1. Where the traffic is today

| Caller | Cluster | Provider and credential today |
| --- | --- | --- |
| learn-ai | applications | OpenAI, Gemini, and TogetherAI static keys, held in SOPS under `src/bridge/secrets/learn_ai/` and written to Vault at `learn_ai/__main__.py:443-470`; Bedrock through IRSA (`:234-279`); Azure OpenAI workload identity, enabled in CI only (`:732-745`) |
| mit-learn | applications | OpenAI static key (`mit_learn/k8s_secrets.py:196`); Azure OpenAI workload identity, CI only (`__main__.py:1639-1652`) |
| edxapp for mitxonline (translation plugins) | applications | OpenAI, Gemini, Mistral, and DeepL static keys in `TRANSLATIONS_PROVIDERS` (`edxapp/k8s_secrets.py:387-411`); Azure identity wired, enabled in no stack |
| dagster `ml` (ol-data-platform) | data | Bedrock through IRSA shared by all code locations (`dagster/__main__.py:405-448`); Vertex through GCP workload identity federation (`:2462-2548`); no static LLM key in the stack |
| gwarek | operations | Bedrock through IRSA, Anthropic models only (`gwarek/__main__.py:322-345`) |

APISIX runs on all four EKS clusters at chart 2.17.0, app version 3.18.0, in standalone mode
(`infrastructure/aws/eks/__main__.py:1276`, `bridge/lib/versions.py:38`). The `ai-*`
plugins are in the allowlist (`apisix_official.py:95-105, 157`) and no route uses them. APISIX has
no Redis or Valkey: rate limiting uses the node-local policy
(`components/services/apisix.py:766-775`). Nothing uses `$secret://` references.

The Azure OpenAI workload-identity PR (ol-infrastructure#5680) merged on 2026-09-10. With
OpenAI traffic moving to Azure OpenAI, the largest share of today's static-key traffic is on
its way to a federated path.

## 2. Coverage rule

What a gateway gives us that the library does not: provider keys held in one place instead of
in each app, token quotas enforced outside application code, one metric source for callers
that are not on the library, header stripping, and gateway-side fallback and caching. The
library already gives every call site tracing, attribution, timeouts, retries, and per-run
limits, whether or not the call passes a gateway.

What each candidate can authenticate with, read from source:

| Provider | APISIX 3.18.0 (unchanged on master) | Agent Router v1.1.0 |
| --- | --- | --- |
| AWS Bedrock | static `access_key_id` and `secret_access_key` only (`ai-proxy/schema.lua:61-70`, `ai-transport/auth-aws.lua:97-101`) | SDK default chain, which covers IRSA and Pod Identity (`internal/backendauth/aws.go:66-73`); or STS web identity |
| Vertex AI | service-account JSON only (`ai-transport/auth.lua:51`); apisix#13732 still open | workload identity federation or ADC (`backendsecurity_policy.go:219-247`) |
| Azure OpenAI | static `api-key` header only | Entra client secret, or an OIDC token exchange. No projected ServiceAccount token path was found, so the federation we deployed in #5680 does not carry over as is |
| OpenAI, Anthropic, Mistral, TogetherAI, Gemini API | static key | static key |

The options considered:

- A: static-key traffic goes through the gateway; federated paths may bypass it. A call
  must use the gateway whenever the alternative is a provider key in the application. A call
  that authenticates with workload identity (Bedrock through IRSA, Vertex through GCP
  federation, Azure OpenAI through workload identity) may go direct until the gateway can
  carry that federation. Bypass paths get their controls from the library: attribution and
  Opik tracing, the call contract (library spec §6), token and cost metrics emitted by the library with the
  same labels the gateway uses, and per-run usage limits. No new long-lived cloud credential
  is created to make a route work.
- B: all model traffic goes through the gateway. The gateway's credential model becomes
  a pass/fail requirement. On APISIX that means minting an IAM user key, a GCP
  service-account key, and an Azure API key into Vault and undoing three federated designs.
  On Agent Router it means proving EKS-to-Azure and EKS-to-GCP federation from the gateway.
- C: the gateway is optional in phase one. The library is the control point. A gateway
  route is used where a team wants central keys or hard quotas, and nothing is required to
  use it.

Decided 2026-10-01 by the project owner: C. The gateway is optional in phase one.

What that means:

- The library is the control point on every path. Attribution, Opik tracing, the call
  contract, and token metrics do not depend on a gateway, and adoption of the library does
  not wait for one.
- No call is required to use a gateway route, and no federated design is undone to make one
  work. Provider keys stay where they are until a team moves a use case onto a route.
- The PoCs still run, APISIX first, as an evaluation of what a gateway adds: central keys,
  quotas enforced outside application code, and a meter for callers that are not on the
  library. The requirements below are what a gateway has to meet to be offered at all.
- A was the recommendation. Its weakness was the reason C is reasonable: once OpenAI traffic
  moves to Azure OpenAI on workload identity, what would have to cross an APISIX gateway
  under A is Gemini API-key, Mistral, TogetherAI, and direct Anthropic traffic, plus whatever
  OpenAI direct use remains.

Where a route is used:

- DeepL is not an LLM API and is out of scope for the gateway.
- A route is offered per provider, not per kind of caller. Batch pipelines, the evaluation
  judge, and embeddings (`llm-usage-policies-spec.md` §4) use it or not on the same terms as
  any other call to that provider.
- Opik's online scoring rules call models from the Opik backend, outside the library. They
  are the one caller a gateway route would meter that the library cannot (`custom-llm`
  provider with a `baseUrl`, `LlmProvider.java:13-21`), and the one caller that cannot use a
  refreshing token.

## 3. Requirements

"Must" fails the candidate. "Weighted" is scored 0 to 3 in §6. With the gateway optional, a
candidate that fails a must is not offered in phase one; it does not block anything else.
Had the rule been "all traffic through the gateway", G2b to G2d would have been must.

| ID | Requirement | Pass criterion | Level |
| --- | --- | --- | --- |
| G1 | Provider coverage | One working route each to OpenAI, Azure OpenAI, Bedrock (an Anthropic model), Vertex Gemini, Mistral, TogetherAI, and Anthropic direct | must for the static-key providers; weighted for Bedrock, Vertex, Azure |
| G2a | Static provider keys from Vault | Provider key lives only in gateway config, sourced from Vault, never in the app | must |
| G2b | Bedrock without a static AWS key | Route works with IRSA or Pod Identity on the gateway's ServiceAccount | weighted |
| G2c | Vertex without a service-account key | Route works with workload identity federation from EKS | weighted |
| G2d | Azure OpenAI without an API key | Route works with Entra federation from EKS | weighted |
| G3 | Streaming with usage | SSE passes through chunk by chunk; token usage is captured on streamed responses | must |
| G4 | Embeddings | OpenAI-format embeddings route works for OpenAI or Azure | must |
| G5 | Protocol fidelity | Tool call, `response_format`, and image input pass intact; the awesome-ai-gateway fidelity probe passes (tool call, multi-chunk SSE with usage, Anthropic client to OpenAI model) | must for the first three; weighted for the cross-protocol case |
| G6 | Fallback and priority routing | A route falls back on 429, 5xx, and timeout, bounded by a retry count; fallback can be turned off per route | weighted |
| G7 | Single retry layer | Gateway retries can be set to zero on a route, and its upstream timeout can be set above the library's (300 s on the batch profile), so the library's policy governs | must |
| G8 | Per-app authentication | Each app-environment has its own credential; a static per-app key works with the OpenAI SDK's `api_key` and PydanticAI by `base_url` alone | must |
| G8b | Keycloak service-account JWT as the app credential | Route accepts a client-credentials JWT and maps it to the app identity | weighted |
| G9 | End-user identity | The pseudonymous user id the library sends (`safety_identifier` or `user`; Anthropic `metadata.user_id`) is available to logs and to rate-limit keys, and never becomes a metric label | weighted |
| G10 | Per-app token quotas | A per-app token quota per time window, with counters shared across gateway replicas, rejecting with 429 | must |
| G11 | Spend derivable | Prompt and completion tokens in Prometheus by app and model, so dollars can be computed in a recording rule | must |
| G12 | `use_case` label | The `X-OL-LLM-Use-Case` header appears as a label on the token metrics | weighted |
| G13 | Trace or log export | Per-request summaries (model, tokens, latency, app) reach Loki or an OTel collector | weighted |
| G14 | No body logging by default | Request and response bodies are not logged unless a route opts in | must |
| G15 | Header hygiene | Client `Authorization`, `Cookie`, and the app credential are not forwarded to the provider | must |
| G16 | Config as code | Routes, consumers, quotas, and credentials are Pulumi-managed | must |
| G17 | Reachability | Callers on the applications, data, and operations clusters can reach a route without leaving the private network | must |
| G18 | Overhead | Against a mock upstream at 50 concurrent requests: added latency p50 at most 5 ms and p99 at most 25 ms non-streaming; added time to first token p99 at most 10 ms; streamed chunks not coalesced | must |
| G19 | Footprint | At that load, at most 512 MiB per replica, with no periodic restart needed | weighted |
| G20 | Operational cost | New control plane or not; upgrade path; how many clusters need it | weighted |

G18's thresholds are proposals. The only independent numbers available are 0.62 ms for
Bifrost and 5.83 ms for the LiteLLM proxy added per request (awesome-ai-gateway, 2026-07),
and no one has measured either shortlisted candidate.

What source reading already says, to be confirmed or overturned by the PoC:

| ID | APISIX 3.18.0 | Agent Router v1.1.0 |
| --- | --- | --- |
| G2b to G2d | none of the three (§2) | AWS and GCP documented; Azure needs an OIDC exchange we have not set up |
| G6 | `ai-proxy-multi` priority and fallback on 429, 5xx, timeout; 3.19.0 adds `fallback_http_statuses` | fallback and priority routing in the route CRD |
| G9 | `$llm_end_user_id` set from the request body (`ai-proxy/base.lua:331-334`); `ai-rate-limiting` rules mode can key on any variable | rate limits key on headers with `type: Distinct`; a JWT claim has to be copied to a header first |
| G10 | `ai-rate-limiting` token quotas per consumer; Redis policy exists but APISIX has no Redis today; default rejection code is 503 and must be set to 429 (`ai-rate-limiting.lua:88-90`); a request is admitted while any quota remains and charged afterward | usage-based global rate limit through Envoy Gateway `BackendTrafficPolicy`; requires Redis; rejects with 429 after the response is charged |
| G11, G12 | `llm_prompt_tokens`, `llm_completion_tokens` by consumer and model; `extra_labels` is wired into every `llm_*` metric (`prometheus/exporter.lua:113-133, 571-644`) but only documented for the HTTP metrics | `gen_ai.client.token.usage` and duration metrics; label options not read |
| G13 | `logging.summaries` adds an `llm_summary` to any logger plugin; `payloads` adds bodies; both default off (`ai-proxy/schema.lua:312-326`) | OTel spans, OpenInference by default, `gen_ai.*` with `AI_GATEWAY_TRACING_SEMCONV=gen_ai`; content opt-in |
| G15 | not done by default: ai-proxy forwards client headers unless a `proxy-rewrite` step strips them | not read |
| G20 | no new control plane; routes on the existing gateways | Envoy Gateway as a second control plane, GA since June 2026 |

## 4. Shortlist

- In: APISIX `ai-proxy` and `ai-proxy-multi`. Already deployed on every cluster, ASF
  governed, Pulumi components exist. The QA gateway runs 3.18.0. 3.19.0 (2026-09-28) carries
  three `ai-proxy` streaming fixes and a breaking change to `ai-proxy-multi` (duplicate
  instance names rejected), so the PoC records which version it ran.
- In: Agent Router (the former Envoy AI Gateway, now `theagentrouter/agent-router`; CRDs
  and the `aigateway.envoyproxy.io` API group unchanged). The only candidate whose source
  shows gateway-side federation for AWS and GCP.
- Out: agentgateway. Single-vendor (Solo.io) with an Enterprise tier, monthly releases
  with breaking changes noted, token buckets only in Kubernetes mode, and absent from the
  independent scorecard.
- Out: Bifrost. Fastest in the one independent benchmark, but OSS budget state is
  per-process memory with multi-replica unsupported, OIDC is Enterprise, releases are
  unsigned, and configuration is JSON or a UI backed by a database.
- Out: LiteLLM proxy. Excluded by the settled constraint on its performance profile, and
  by its 2026 advisory record.
- Kong OSS, Portkey, Helicone, and TensorZero were ruled out in the direction assessment.

The reasons for the three exclusions are from the direction assessment (2026-09-10) and were
not re-read. agentgateway and Bifrost come back only if both shortlisted candidates fail a
must.

## 5. PoC acceptance criteria

Each PoC is done when every row of §6 has a measured value or a stated reason it could not
be measured, and the work can be torn down from Pulumi alone.

APISIX PoC (`tk-apisix-ai-proxy-poc-on-the-qa-gateway-with-one-r-d0b512`), on the QA
applications-cluster gateway:

1. One OpenAI-format route with a consumer per caller, key-auth reading `Authorization`,
   the provider key as a Vault secret reference, and a `proxy-rewrite` that strips client
   headers.
2. One real route each for Azure OpenAI, Bedrock, and Vertex using static credentials held
   only for the PoC, to record what G2b to G2d cost on APISIX.
3. `ai-rate-limiting` with the Redis policy against a Valkey added for the PoC, rejection
   code 429, exercised with two gateway replicas.
4. `extra_labels` carrying `use_case` on the `llm_*` metrics, read back from Prometheus.
5. One low-risk caller pointed at the route (mit-learn credential metadata is the smallest
   request-time path).

Agent Router PoC (`tk-agent-router-poc-in-a-scratch-namespace-on-the-q-3a178c`), in a
scratch namespace on the QA data cluster:

1. Envoy Gateway and Agent Router installed from Pulumi, with one OpenAI-format route.
2. Bedrock through the gateway ServiceAccount's IRSA role, Vertex through workload identity
   federation, and Azure OpenAI through whichever Entra path works from EKS, each with no
   static cloud key.
3. Usage-based rate limiting per app header with Redis, exercised with two replicas.
4. `gen_ai` metrics and spans exported to the existing collector.

Benchmark harness (`tk-build-the-gateway-benchmark-harness-overhead-and-a1eb2c`): one mock
upstream, both gateways and a direct baseline, interleaved rounds, G18 and G19 measured
streaming and non-streaming, and the three-case fidelity probe for G5.

## 6. Scoring sheet

Filled in by the PoC tasks. Must rows take pass or fail. Weighted rows take 0 (absent), 1
(possible with custom work), 2 (works with caveats), or 3 (works as configured).

| ID | Level | APISIX | Agent Router | Evidence (link or command) |
| --- | --- | --- | --- | --- |
| G1 static-key providers | must | | | |
| G1 Bedrock, Vertex, Azure | weighted | | | |
| G2a | must | | | |
| G2b | weighted | | | |
| G2c | weighted | | | |
| G2d | weighted | | | |
| G3 | must | | | |
| G4 | must | | | |
| G5 same-protocol | must | | | |
| G5 cross-protocol | weighted | | | |
| G6 | weighted | | | |
| G7 | must | | | |
| G8 | must | | | |
| G8b | weighted | | | |
| G9 | weighted | | | |
| G10 | must | | | |
| G11 | must | | | |
| G12 | weighted | | | |
| G13 | weighted | | | |
| G14 | must | | | |
| G15 | must | | | |
| G16 | must | | | |
| G17 | must | | | |
| G18 | must | | | |
| G19 | weighted | | | |
| G20 | weighted | | | |

For the decision record (`tk-gateway-decision-record-poc-teardown-and-retirem-d5d632`): a
candidate that fails a must is out. If both pass, compare the weighted totals with and
without G2b to G2d. Agent Router has to lead on both to justify a second control plane on
the scores alone. If it leads only through the federation rows, the decision is how much
federation at the gateway is worth, and that goes back to the project owner with the sheet.
