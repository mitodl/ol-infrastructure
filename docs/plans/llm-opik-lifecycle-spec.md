# Spec: Opik access, prompt lifecycle, and evaluation on the shared instance

Tracked as workflow project `wp-platform-supported-llm-capabilities-ecca0b`. Each part
answers one decision task. All five were decided by the project owner on 2026-10-01.

| Part | Decision task | Status |
| --- | --- | --- |
| §1 Access model | `tk-decide-the-opik-access-model-for-a-shared-instan-fb80c2` | Decided: Keycloak-only with a group gate, per-app clients, per-app projects |
| §2 Prompt authoring and approval | `tk-decide-the-prompt-authoring-surface-and-approval-e71d7f` | Decided: split by label (option C) |
| §3 Prompt runtime policy | `tk-decide-prompt-overrides-cache-behavior-fallback--5e7c55` | Decided as written |
| §4 Mandatory surfaces and deepeval | `tk-decide-which-opik-surfaces-are-mandatory-and-whe-873bd5` | Decided: tiered list; deepeval replaced by Opik metrics |
| §5 Evaluation gating | `tk-decide-evaluation-gating-what-runs-where-thresho-592e9d` | Decided: advisory everywhere at first |

Settled inputs (2026-09-10): Opik is the platform investment and the prompt system of record;
production Opik becomes the shared instance across app environments; QA Opik stays for
upgrade testing; LangSmith is retired; every LLM call site goes through the shared library
(`shared-llm-library-spec.md`).

Source read on 2026-10-01: comet-ml/opik `3de1c5e` (2.2.88 unreleased) with tags 2.2.87 and
2.2.81; ol-infrastructure `eb2c4b6`; learn-ai `9f29bda`; mit-learn `b6d0e97`; open-edx-plugins
`50483d3`; ol-data-platform `f6d38b2`. Backend paths are under
`apps/opik-backend/src/main/java/com/comet/opik`, SDK paths under `sdks/python/src/opik`.
Prompt fetch, label resolution, and trace linkage were also run against a local Opik 2.2.87.

## 1. Access model

### 1.1 What exists

- Opik's own authentication cannot be turned on in a self-hosted install.
  `authentication.enabled` switches the backend to `RemoteAuthService`, which calls a
  `react-svc` service (`RemoteAuthService.java:283-679`) that is not in the repository, the
  chart, or the compose file.
- With it off, every request is user `admin` in workspace `default`. The `Authorization`
  header is never read, any other `Comet-Workspace` value gets a 404
  (`AuthService.java:45-58`), and `LocalWorkspacePermissionsService` returns no restrictions.
  There are no API keys, no second workspace, no read-only users, and no per-project
  permissions. Projects are the only partition.
- Our gate is APISIX with Keycloak: realm `ol-platform-engineering`, confidential client
  `ol-opik-client`, bearer JWT for SDK traffic and a session cookie for the UI
  (`applications/opik/__main__.py:426-538`). Admission is any authenticated principal in the
  realm. The plugin config has no group, role, or audience check
  (`components/services/apisix.py:688-720`). Whether the bearer route accepts a token minted
  for a different client in the realm is not verified.
- learn-ai and the Dagster `ml` code location both authenticate as `ol-opik-client`'s service
  account, from the one secret at Vault `secret-operations/sso/opik`.
- Each environment's apps point at that environment's Opik today (`opik_stack` is resolved by
  stack name in both `learn_ai/__main__.py:120` and `dagster/__main__.py:144-148`). Chart
  2.2.81 is deployed (`bridge/lib/versions.py:73`).

So anyone admitted can edit any prompt, move any label, delete any dataset, and read every
trace, and Opik cannot change that.

### 1.2 Decision

1. Keycloak-only access, with the controls below. It is the only model OSS Opik supports
   short of running separate instances.
2. Restrict who is admitted. UI access requires membership in a Keycloak group
   (`opik-users`), enforced either in Keycloak (a required client role on `ol-opik-client`)
   or by a claim check at APISIX. The bearer route additionally checks the token audience.
   APISIX 3.18.0's `claim_validator.audience.match_with_client_id` compares `aud` with the
   route's own `client_id` (`openid-connect.lua:1254-1290`), which is `ol-opik-client`, so
   every per-app client in item 3 needs a Keycloak audience mapper that adds
   `ol-opik-client` to its tokens. A token without it gets a 403.
   Neither mechanism has been tested; the implementation task picks one.
3. One service identity per application and environment. Each app-environment gets its own
   Keycloak client for SDK traffic (for example `opik-learn-ai-production`), replacing the
   shared secret, so a credential can be revoked without touching other apps and APISIX and
   Keycloak logs say which app wrote. The CI prompt-sync and evaluation jobs get their own
   client too.
4. One Opik project per application, named for the app (`learn-ai`, `mit-learn`,
   `dagster-ml`, `gwarek`, `open-edx-translations`). Environment is carried on the trace
   (`environment` field plus the `env:` tag, attribution schema §4.1), not in the project
   name. Prompts and datasets live in the app's project.
5. Controls on prompts and labels are detective, not preventive, because Opik cannot
   prevent. §2 says who is meant to move labels, and a scheduled job reports any production
   label that does not match the version the app repository points at.
6. QA Opik takes no application traffic once apps are rewired. It gets upgrades first.

The cost of item 4: Opik retention rules are scoped to organization, workspace, or project
(`RetentionLevel.java:15-17`), so one project per app means CI and QA traces are kept as long
as that app's production traces. Per-environment projects (`learn-ai-qa`) would allow a
shorter period for non-production traces and let online scoring rules select by project, at
the cost of splitting each app's traces across three projects. Per-app projects were chosen:
non-production volume is small and the simpler layout is worth more.

Decided 2026-10-01: items 1 to 6, with per-app projects.

## 2. Prompt authoring and approval

### 2.1 What exists

Opik keeps immutable sequential versions per prompt name, and environment labels that sit on
exactly one version each and can be moved (`set_prompt_environments`,
`opik_client.py:2686`). It has no review state and, per §1, no way to restrict who edits.

Three integrations already let someone change a prompt without a code deploy, each its own
way:

- ol-data-platform renders the Opik Prompt Library entry `feedback-summary`, creating it from
  the code default if missing, and takes the latest version with no label
  (`ml/resources/opik_auth.py:268-293`). An edit in the Opik UI reaches the next production
  run. The pipeline records the version that produced each row (`ml/lib/summarize.py:30-38`).
- mit-learn keeps the credential-metadata prompts, model, and temperature in admin-editable
  rows with no version history (`learning_resources/models.py:1692-1727`,
  `admin.py:319-330`); the generation log snapshots the prompt text per call.
- learn-ai pulls prompts from LangSmith by an environment-keyed name when a key is set,
  caches them in Redis for 28 days (`main/settings.py:636-638`), and pushes with a management
  command (`management/commands/update_prompt.py:84-118`). Staff can replace the system prompt per request (§3).

The other prompts are constants in code (the translation plugins, gwarek, mit-learn's
summary, flashcard, and OCR prompts).

### 2.2 Options

- A: the repository is the source. A prompt is a file in the app's repo next to the code that
  uses it. A pull request is the approval. On merge, a CI job creates the Opik version
  (`create_prompt` is a no-op when nothing changed) and moves the `ci` label; the deploy
  pipeline moves `qa` and `production` as that code reaches each environment. A prompt-only
  change ships through the same job without an application deploy. The Opik UI and
  Playground are for trying variants; a variant becomes real by being copied into the file.
  Humans do not move labels, and the drift check (§1.2 item 5) reports it if one does.
- B: Opik is the source. People edit in the UI and move labels by hand. Fastest, and what
  ol-data-platform effectively does today. There is no review, no gate, and no record of who
  approved what beyond the version history.
- C: split by label. Anyone admitted may create versions and move non-production labels in
  the UI. Only CI moves `production`, from a merged pull request that names the prompt and
  the version number to promote. The repository holds a pointer, not the text, so the
  embedded fallback (§3) has to be exported at build time.

### 2.3 Decision

Decided 2026-10-01: C. A was the recommendation; C keeps direct editing for the people who
tune prompts and puts the control on the one label that matters.

The workflow:

1. Anyone admitted to Opik (§1) creates versions in the UI or Playground and may move the
   `ci` and `qa` labels.
2. Each app repository holds a pointer file: for every prompt, the name and the version
   number that production runs. Changing production means a pull request that changes the
   number.
3. The pull request runs the evaluation for that version (§5) and links the experiment.
4. On merge, a CI job moves the `production` label to the pointed version. Promotion jobs
   for a repository run one at a time, and each reads the pointer file from the default
   branch's HEAD when it starts, not from the commit that triggered it. Otherwise two merges
   close together can finish out of order and the older job moves the label back. Only that
   job's Keycloak client is meant to move it. Opik cannot enforce that, so the drift report
   (§1.2 item 5) compares the label with the pointer and reports a mismatch.
5. Rollback is a revert of the pointer change.
6. The build exports the text of the pointed version into the image as the embedded default
   (§3 item 4), so the fallback is a reviewed version.

Two consequences:

- A promotion that changes only the pointer moves the label without rebuilding the image, so
  until the next build the embedded default is one version behind. During an Opik outage at
  cold start the app then runs the previous production prompt, marked degraded.
- ol-data-platform changes from "latest version, created from code if missing" to fetching
  by label with a pointer in the repo. mit-learn's admins and learn-ai's prompt authors edit
  in Opik and promote through a pull request.

## 3. Prompt runtime policy

The rules the library's prompt helper implements. Decided 2026-10-01 as written, read with
§2's option C: the embedded default is the pointed production version exported at build time.

1. Deployed code fetches by environment label:
   `get_prompt(name, environment=<ci|qa|production>)`, with the app's project. Opik seeds
   only `development`, `staging`, and `production`, and moving a label to an unregistered
   name fails (measured: `EnvironmentNotFoundError`), so `ci` and `qa` are created once per
   instance. Local development uses the embedded default and does not need Opik.
2. Version pins are for batch runs and evaluations, not for request-time code. A batch run
   resolves the label once at the start and uses that version for the whole run.
3. Cache: keep the SDK default of 300 s (`OPIK_PROMPT_CACHE_TTL_SECONDS`,
   `config.py:391-396`). A label move reaches every process within five minutes. A warm
   entry keeps being served if a refresh fails (`prompt_cache.py:142-161`).
4. Cold start with Opik unreachable: fall back to the embedded default, which is the text of
   the pointed production version exported into the image at build time (§2.3), and mark the
   result degraded with reason `embedded_prompt`
   (call contract §6.7). The SDK's own failure took 11 s to surface in the local test, so the
   helper puts a 2 s deadline on a cold fetch. This applies to both profiles: a batch run
   does not fail because Opik is down.
5. Batch idempotency keys include the prompt, keyed on a hash of the template text, not on
   the Opik version number. The version number is recorded alongside for linkage. A run that
   fell back to the embedded default then produces the same key as one that fetched the same
   text, so an Opik outage does not trigger reprocessing. ol-data-platform already
   re-summarizes on a `prompt_version` change (`ml/lib/summarize.py:225-290`) and records
   `"local"` when Opik is unavailable, which would reprocess every row once Opik came back.
6. Variables are not overrides. Data injected into a prompt (the translation glossary,
   retrieved context, resource facts) is a template variable. The trace records the canonical
   prompt version and the variables as input.
7. Per-request overrides are a debugging tool. learn-ai's staff-only `instructions` field
   replaces the system prompt (`ai_chatbots/serializers.py:54-60`, `chatbots.py:95-97`) and
   leaves nothing on the trace saying so. It may stay, staff-only, if the trace carries
   `prompt_override=true` and the name and version it replaced, and overridden traces are
   excluded from datasets and online scoring.
8. Configuration rows that hold prompt text are prompt management by another name.
   mit-learn's `CredentialMetadataConfiguration.prompt` and `retrieval_query` move to
   Opik prompts, edited there and promoted by pointer; `llm_model` and `temperature` move to
   the use-case declaration.
9. Templates are mustache unless a prompt needs conditionals or loops, in which case it is
   declared `jinja2`. Opik's mustache is a `{{key}}` substitution, not the full language
   (`prompt/text/prompt_template.py:27-67`).
10. Text or chat is fixed per prompt name and cannot change later (`client.py:105-111`). A
    system or instructions string is a text prompt, which is what a PydanticAI agent's
    `instructions` takes. A multi-message template is a chat prompt.
11. Names are `<use_case>` or `<use_case>.<part>` within the app's project, matching the
    attribution `use_case` so traces and prompts join by name.

Decided 2026-10-01: items 1 to 11. In particular, neither profile fails on an Opik outage,
batch work is keyed on prompt content, learn-ai's staff override stays and is marked on the
trace, and mit-learn's configuration-row prompts move to Opik.

## 4. Mandatory Opik surfaces and deepeval

### 4.1 What exists

- Traces reach Opik from learn-ai and, since 2026-09-14, from the Dagster `ml` code location.
  No one uses datasets, experiments, feedback scores, or online scoring rules.
- learn-ai's evaluation harness is the only model-graded evaluation in the fleet
  (`ai_chatbots/evaluation/`). It uses deepeval (`deepeval>=4.0.7` in the dev group, locked
  4.2.0): Faithfulness, ContextualRelevancy, Hallucination, AnswerRelevancy, and a GEval
  "Helpfulness" by default, with ContextualPrecision and ContextualRecall when expected
  outputs exist (`orchestrator.py:76-185`). The judge is the string `gpt-4o-mini` passed to
  each metric (`management/commands/rag_evaluation.py:26-31`), so it calls OpenAI directly, untraced. It runs
  from `manage.py rag_evaluation` against JSON files in the repo, never in CI, and can upload
  results to Confident AI when `CONFIDENT_AI_API_KEY` is set (`orchestrator.py:197`).
- learn-ai stores per-message ratings (`ChatResponseRating`, `ai_chatbots/models.py:114-142`) and nothing
  forwards them to Opik.
- The translation plugins have deterministic validators: SRT timestamps
  (`providers/base.py:103-148`), brace-placeholder preservation in the static-strings plugin
  (`utils.py:444-470`), and grading-type uniqueness (`llm_providers.py:1015-1035`).
- Opik's SDK ships judge metrics that overlap deepeval's (Hallucination, AnswerRelevance,
  ContextPrecision, ContextRecall, GEval, Usefulness) and heuristic ones (Equals, RegexMatch,
  IsJson, ROUGE, and others). `evaluate()` takes any `BaseMetric` subclass or scoring
  function (`evaluator.py:138-161`), so a third-party metric can be wrapped. The judge is a
  LiteLLM model name or an `OpikBaseModel` instance (`models_factory.py:36-90`). Datasets are
  versioned and an experiment records the dataset version and prompt versions it ran against.

### 4.2 Decision

Mandatory for every integration:

- traces for every call, with the attribution schema;
- every prompt in the Prompt Library, fetched through the library.

Mandatory for any use case whose output a learner or instructor sees:

- a golden dataset in Opik and an experiment on every prompt or model change (§5; the gate
  starts advisory);
- user ratings forwarded as feedback scores where the product collects them. For learn-ai
  that is `ChatResponseRating` to `log_traces_feedback_scores`, which needs the trace id kept
  with the checkpoint.

Optional per use case: online scoring rules on sampled production traces.

deepeval: replace it with Opik's metrics, and keep the wrapper path for any metric with no
equivalent.

- One metric library means one judge configuration and one place scores land. The judge
  becomes an `OpikBaseModel` backed by the shared library, so judge calls are traced,
  attributed (`use_case=eval-judge`), and routed like any other call. That closes the one
  path where learn-ai calls a model outside every control.
- Hallucination, AnswerRelevance, ContextPrecision, ContextRecall, and GEval have direct
  Opik counterparts. Faithfulness and ContextualRelevancy do not have a same-named one; the
  learn-ai team either maps them (Hallucination is the nearest to Faithfulness) or wraps the
  deepeval metric as a `BaseMetric`.
- Scores from different libraries are not comparable, so learn-ai's thresholds are
  re-baselined. The harness has never gated a change, so nothing depends on the old numbers.
- The Confident AI upload goes away with it. It is a second external sink for evaluation
  content.

Decided 2026-10-01: the tiered list above, and deepeval is replaced by Opik's metrics with
the wrapper path for gaps.

## 5. Evaluation gating

Nothing gates a prompt or model change today.

### 5.1 Decision

Decided 2026-10-01: the mechanics below, with every gate advisory at first (item 4).

1. An evaluation is required when a use case's prompt text, model, fallback list, or
   retrieval and tool configuration changes.
2. It runs in CI on the pull request that makes the change, against the shared Opik. For a
   prompt that is the pull request changing the pointer (§2.3): the job fetches the pointed
   version, runs `evaluate(prompts=[...])` on the use case's dataset with `environment=ci`,
   and links the experiment in the pull request. A model or configuration change runs the
   same job against the current production prompt.
3. Each use case declares its gate in the repository, next to its prompt: the dataset name
   and pinned dataset version, the metrics, the judge model, and thresholds. The owning app
   team sets them.
4. A gate is `blocking` or `advisory`. Every gate starts advisory: it reports and does not
   fail the check. A use case's owner switches it to blocking once they have seen enough runs
   to trust the thresholds. Blocking by default for learner-facing use cases was the
   recommendation and was not adopted.
5. Pass means: every deterministic validator passes on every item, and no judged metric's
   mean falls more than the declared tolerance below the baseline, where the baseline is the
   last experiment for the version carrying the `production` label. The first run for a use
   case sets the baseline.
6. Golden datasets are owned by the app team and curated in Opik. The gate declaration pins
   the dataset version, so changing what a use case is evaluated against is a reviewed
   change to that pin.
7. The judge follows §4: called through the shared library, traced, cost-attributed, and
   routed like any other model call. It should not be the model
   under test.
8. Deterministic validators are metrics. The translation checks become `BaseMetric`
   subclasses used in experiments and stay as runtime checks in the plugin.
9. Online scoring is the second line, not the gate. Rules run in the Opik backend with
   provider keys stored in Opik (`LlmProviderFactoryImpl.java:55-86`), sampled per project.
   A rule cannot filter on the trace environment field
   (`TraceFilterEvaluationService.java:71-95`), so production-only rules filter on the
   `env:production` tag. These judge calls happen outside the shared library. If a gateway
   route exists they use it with their own consumer so they are metered; otherwise their
   spend shows up only on the provider invoice.

Not settled here: where the CI job runs (GitHub Actions needs a route to Opik and model
credentials; an in-cluster job does not) and what it costs per run. Both belong to
`tk-implement-the-ci-evaluation-job-template-using-o-cd0061`.

Decided 2026-10-01: items 1 to 9 as amended above.
