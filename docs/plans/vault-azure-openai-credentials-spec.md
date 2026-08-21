# Spec: Vault-managed Azure OpenAI credentials (mit-learn, learn-ai, edxapp)

Tracked as workflow project `wp-vault-managed-azure-openai-credentials-for-mit-l-cd2801`,
where the implementation task breakdown and dependency graph live.

**This document is the source of truth.** It supersedes an earlier uncommitted planning
document, whose assumptions were verified against the codebase here — several did not hold
(see §2.1) — and whose gaps are resolved or flagged below. That earlier document is not in
the repo and should not be consulted; anything from it that still matters was carried into
this spec.

All file references below are repo-relative from the repository root.

## 1. Goal

Deliver Azure-AD-based, Vault-minted, short-lived credentials for Azure OpenAI to three
consumers (mit-learn, learn-ai, edxapp/mitxonline) **alongside** their existing static
`OPENAI_API_KEY` wiring, which is not touched. This repo provisions infrastructure and
delivers credentials + endpoint config into the pod environment; making the applications
actually *use* those credentials is out of scope (separate PRs in each app repo).

## 2. Verified against the codebase

Everything below was checked in the working tree, not assumed.

| Assumption | Status |
| --- | --- |
| `pulumi_vault.azure.Backend` / `BackendRole` / `BackendRoleAzureRoleArgs` exist in the installed `pulumi-vault` | **Confirmed.** `.venv/.../pulumi_vault/azure/{backend,backend_role,_inputs}.py` |
| `Backend` accepts `subscription_id`, `tenant_id`, `client_id`, `client_secret`, `environment`, `path`, `description` | **Confirmed.** `subscription_id`/`tenant_id` are required positional-by-keyword |
| `BackendRole` accepts `azure_roles`, `backend`, `role`, `ttl`, `max_ttl`, `explicit_max_ttl`, `sign_in_audience`, `permanently_delete`, `tags` | **Confirmed.** Role identifier arg is `role=`, **not** `name=` |
| `BackendRoleAzureRoleArgs` fields | **Confirmed:** `role_id`, `role_name`, `scope` |
| `pulumi-azure-native` / `pulumi-azuread` present | **Not installed.** Must be added to `pyproject.toml` + `uv lock && uv sync` |
| AWS secrets-engine component to mirror | **Confirmed** at `src/ol_infrastructure/components/services/vault.py:238-291` |
| mit-learn `_create_dynamic_secret` helper + AWS example | **Confirmed** at `src/ol_infrastructure/applications/mit_learn/k8s_secrets.py:79` and `:313-328` |
| learn-ai dynamic-secret + `OLVaultK8SResources` block | **Confirmed** at `src/ol_infrastructure/applications/learn_ai/__main__.py:640-690` |
| edxapp `VaultSecretBuilder.create_static/create_dynamic` | **Confirmed** at `src/ol_infrastructure/applications/edxapp/secrets_factory.py:77` (static) and `:121` (dynamic — returns a `Callable` for use with `Output.apply`) |
| Azure is greenfield in this repo | **Confirmed** — no Azure provider config anywhere |

### 2.1 Deviations from the plan that must be honoured in implementation

1. **`ttl` / `max_ttl` on `azure.BackendRole` are Go duration *strings*** (`"24h"`), not
   integer seconds like `OLVaultAWSSecretsEngineConfig.default_lease_ttl_seconds`. The new
   config model must not copy the AWS component's int-seconds typing.
2. **`azure.BackendRole` uses `role=` for the role name**, unlike `aws.SecretBackendRole`
   which uses `name=`. Copy-paste from the AWS loop will silently create wrongly-named roles.
3. **edxapp config sources are concatenated, not merged.** See §5.3 — this invalidates the
   plan's implied approach of extending `TRANSLATIONS_PROVIDERS`.
4. **Two separate Azure identities are required**, not one. See §3.
5. **Concourse + project-registry wiring is required** for both new Pulumi projects. The
   plan omitted this. See §6.

## 3. Azure identity model (the plan conflated these — they are distinct)

| Identity | Created by | Permissions | Consumed by |
| --- | --- | --- | --- |
| **Pulumi deploy SP** | **Manually, once, out of band** (bootstrap) | `Contributor` + `User Access Administrator` (or `Owner`) on the target subscription — needs to create resource groups, Cognitive Services accounts, app registrations, and role assignments. Plus Graph `Application.ReadWrite.All` to create the Vault root app registration. | `pulumi_azure_native.Provider` / `pulumi_azuread.Provider` in `src/ol_infrastructure/infrastructure/azure/openai` |
| **Vault root SP** | **By Pulumi** (`azuread.Application` + `ServicePrincipal` + `ApplicationPassword`) | Graph `Application.ReadWrite.OwnedBy` (application permission, **requires tenant admin consent**) + `User Access Administrator` scoped **only to the new resource group** | Vault's Azure secrets engine (`azure.Backend` `client_id`/`client_secret`) |
| **Per-app dynamic SPs** | **By Vault at request time** | `Cognitive Services OpenAI User` scoped to the Cognitive Services account | mit-learn / learn-ai / edxapp pods |

**Bootstrap credential storage.** This repo's established pattern for non-AWS provider
credentials is SOPS-encrypted YAML read via `bridge.secrets.sops.read_yaml_secrets`
(see `src/ol_infrastructure/infrastructure/qdrant_cloud/__main__.py:17` and `src/ol_infrastructure/infrastructure/mongodb_atlas/__main__.py:64`).
Follow it: new files `src/bridge/secrets/pulumi/azure.{ci,qa,production}.yaml` holding
`tenant_id`, `subscription_id`, `client_id`, `client_secret` for the **Pulumi deploy SP**.
`.sops.yaml`'s existing `path_regex` rules already cover `*.ci.yaml` / `*.qa.yaml` /
`*.production.yaml`, so no `.sops.yaml` change is needed.

## 4. Resource specification

### 4.1 `src/ol_infrastructure/infrastructure/azure/openai/`

New standalone Pulumi project. Layout mirrors `src/ol_infrastructure/infrastructure/qdrant_cloud/`:
`Pulumi.yaml` (name `ol-infrastructure-azure-openai`, backend `s3://mitol-pulumi-state/`),
`Pulumi.{CI,QA,Production}.yaml`, `__init__.py`, `__main__.py`, `README.md`.

Provisions, per environment (layout decided in Q5 — one account per app consumer):

- `azure_native.resources.ResourceGroup` — `ol-openai-{env}`, one per environment
- `azure_native.cognitiveservices.Account` — **one per app consumer**, named
  `ol-openai-{consumer}-{env}` for `consumer in {mitlearn, learn-ai, mitxonline}`.
  `kind="OpenAI"`, `sku="S0"`, `custom_sub_domain_name` set (required for AAD token auth —
  without it only key auth works), `public_network_access="Enabled"`, `disable_local_auth`
  left **false** for now (additive migration; key auth stays available)
- `azure_native.cognitiveservices.Deployment` — `gpt-4o`, `gpt-4o-mini`, `gpt-5.2` (Q4) on
  each account. Capacity is allocated per deployment and per environment: Production gets the
  bulk, CI/QA the minimum that still exercises the path, because all 27 deployments draw from
  one shared regional TPM pool (Q1/Q5).
- `azuread.Application` + `ServicePrincipal` + `ApplicationPassword` for the Vault root SP,
  **one per environment**, requesting Microsoft Graph `Application.ReadWrite.OwnedBy` (app
  permission)
- `azure_native.authorization.RoleAssignment` — Vault root SP → `User Access Administrator`,
  `scope = resource_group.id` (**not** subscription scope), so one grant covers all three of
  that environment's accounts

Stack outputs (consumed by the substructure project via `StackReference`):
`resource_group_id`, `cognitive_accounts` (a map of consumer → `{id, endpoint}`),
`model_deployments`, `vault_root_sp_client_id`, `vault_root_sp_tenant_id`,
`vault_root_sp_subscription_id`, `vault_root_sp_client_secret` (wrapped in
`pulumi.Output.secret(...)`).

**Manual step, documented in the project README, not automated:** a tenant Global Admin must
run `az ad app permission admin-consent --id <app-id>` once per environment. Pulumi can create
the permission *request* but cannot grant consent non-interactively. All Pulumi resources apply
successfully without consent; only credential *issuance* (§4.3) fails until it is granted.

### 4.2 `OLVaultAzureSecretsEngine` component

Added to `src/ol_infrastructure/components/services/vault.py`, immediately after
`OLVaultAWSSecretsEngine` (~line 291). Add `azure` to the `from pulumi_vault import ...`
line at `src/ol_infrastructure/components/services/vault.py:21`.

```
OLVaultAzureRoleConfig(BaseModel)
    role_name: str            # e.g. "Cognitive Services OpenAI User"
    scope: str | Output[str]  # cognitive_account_id
    ttl: str = "24h"          # duration STRING, not seconds
    max_ttl: str = "48h"

OLVaultAzureSecretsEngineConfig(BaseModel)
    app_name: str
    subscription_id / tenant_id / client_id / client_secret: str | Output[str]
    vault_backend_path: str = "azure-openai"   # same `is_valid_path` validator as AWS
    description: str
    default_lease_ttl_seconds / max_lease_ttl_seconds: int   # Mount-level, still ints
    roles: dict[str, OLVaultAzureRoleConfig]

OLVaultAzureSecretsEngine(ComponentResource)
    type token: "ol:services:Vault:AzureSecretsEngine"
    -> one azure.Backend, then a loop over `roles` creating one azure.BackendRole each
```

### 4.3 `src/ol_infrastructure/substructure/vault/azure/`

New project, sibling to `secrets`/`pki`/`static_mounts`. `Pulumi.yaml` name
`ol-substructure-vault-azure`; stacks `operations.{CI,QA,Production}` (matching every other
`src/ol_infrastructure/substructure/vault/*` project).

`__main__.py`:
- `StackReference("…/ol-infrastructure-azure-openai/{env}")`
- instantiate `OLVaultAzureSecretsEngine` at mount `azure-openai` with three roles:
  `ol-mitlearn-openai`, `ol-learn-ai-openai`, `ol-mitxonline-openai`. Each role is scoped to
  **its own consumer's** account, not to the resource group:
  `azure_roles=[{role_name: "Cognitive Services OpenAI User", scope: cognitive_accounts[consumer].id}]`.
  That scoping is what stops one app's dynamic credentials from reaching another app's endpoint.
- export `azure_openai_mount_path`, `cognitive_account_endpoints` (consumer → endpoint),
  `model_deployments`

## 5. Per-app wiring (additive)

### 5.1 mit-learn

- `src/ol_infrastructure/applications/mit_learn/mitlearn_policy.hcl`: add
  `azure-openai/creds/ol-mitlearn-openai` and `.../ *` read grants; extend both
  `sys/leases/renew` and `sys/leases/revoke` `allowed_parameters.lease_id` lists with
  `azure-openai/creds/ol-mitlearn-openai/*` (VSO cannot renew/revoke otherwise).
- `src/ol_infrastructure/applications/mit_learn/k8s_secrets.py`: one new `_create_dynamic_secret(...)` call —
  `mount="azure-openai"`, `path="creds/ol-mitlearn-openai"`, templates
  `AZURE_OPENAI_CLIENT_ID` ← `client_id`, `AZURE_OPENAI_CLIENT_SECRET` ← `client_secret`.
  Append to `secret_names` / `secret_resources` like the AWS block at `:313`.
- `src/ol_infrastructure/applications/mit_learn/__main__.py`: plain (non-secret) container env vars
  `AZURE_OPENAI_ENDPOINT`, `AZURE_OPENAI_TENANT_ID`, `AZURE_OPENAI_API_VERSION`.

### 5.2 learn-ai

Same three pieces, in `src/ol_infrastructure/applications/learn_ai/learn_ai_policy.hcl` (role
`ol-learn-ai-openai`) and a new `OLVaultK8SSecret`/`OLVaultK8SDynamicSecretConfig` block in
`src/ol_infrastructure/applications/learn_ai/__main__.py` modelled on the db-creds block at `:657-690`. Include
`restart_target_kind="Deployment"` / `restart_target_name="learn-ai-app"` so credential
rotation restarts the app, matching the db-creds secret.

`AI_DEFAULT_*_MODEL` values (`openai/gpt-4o-mini`, `openai/gpt-4o` in all three stack
YAMLs) are **not** changed in this pass.

### 5.3 edxapp / mitxonline — revised 2026-08-10 after reading the plugin source

**Finding that changes the plan:** edxapp does not deep-merge its config sources. The init
container runs `cat /openedx/config-sources/*/*.yaml > /openedx/config/lms.env.yml`
(`src/ol_infrastructure/applications/edxapp/k8s_resources.py:639`, `:950`, and the CronJob variants at `:1202`,
`:1322`, `:1437`, `:1539`). Files are **concatenated into one YAML document**.

Consequence: the existing `TRANSLATIONS_PROVIDERS:` mapping is emitted by the *static*
`14-translations-providers-secrets.yaml` secret (`src/ol_infrastructure/applications/edxapp/k8s_secrets.py:386-411`). Azure credentials
come from a *different Vault mount* and therefore a *different* VSO secret and file. A second
file also emitting a top-level `TRANSLATIONS_PROVIDERS:` key would produce a duplicate key —
last-one-wins, silently clobbering the deepl/openai/gemini/mistral providers.

Therefore the Azure creds for edxapp **must** be delivered as new, distinct top-level settings
in their own file. Existing numbers are `10-general`, `11-xqueue`, `12-forum`,
`13-canvas-syllabus-token`, `14-translations-providers`, `15-meilisearch`, `16-typesense`, and
— **correcting the earlier draft of this spec, which claimed 17 was free** —
`17-webhook-tokens`. The Azure file is therefore **`18-azure-openai-secrets.yaml`**:

```yaml
AZURE_OPENAI_CLIENT_ID: ...
AZURE_OPENAI_CLIENT_SECRET: ...
AZURE_OPENAI_TENANT_ID: ...
AZURE_OPENAI_ENDPOINT: ...
AZURE_OPENAI_API_VERSION: ...
AZURE_OPENAI_DEFAULT_DEPLOYMENT: ...
```

Flat `SCREAMING_SNAKE` top-level keys match the convention every other config source in this
directory already follows (`COMMENTS_SERVICE_KEY`, `MEILISEARCH_MASTER_KEY`,
`TYPESENSE_API_KEY`, `CERTIFICATE_WEBHOOK_ACCESS_TOKEN`), and they avoid the duplicate-key
clobbering described above because no other file emits them.

**Finding: delivering these settings is necessary but not sufficient.** The translations
plugins read provider credentials from exactly one place —
`settings.TRANSLATIONS_PROVIDERS[provider_name]["api_key"]`, in
`ol_openedx_course_translations/utils/course_translations.py:64-76` — and dispatch to one of
three hardcoded classes (`OpenAIProvider`, `GeminiProvider`, `MistralProvider` in
`providers/llm_providers.py`). There is no code path that reads flat `OPENAI_*` settings, and
no Azure provider exists. So the plugin work is not "point an existing setting at Azure"; it is:

1. a new `AzureOpenAIProvider(LLMProvider)` — the base class builds LiteLLM model strings as
   `f"openai/{model}"` / `f"gemini/{model}"`, so Azure needs `f"azure/{deployment_name}"`;
2. `_call_llm` passes only `api_key=`, but Azure AD auth needs `api_base`, `api_version`, and a
   **bearer token**, not a key. Vault issues a `client_id`/`client_secret` pair, so the plugin
   must exchange those for a token (`azure-identity`'s `ClientSecretCredential`) and pass it as
   LiteLLM's `azure_ad_token`. This exchange is the real work and belongs in the plugin;
3. `apply_common_settings` (`settings/common.py:39`) composes the `TRANSLATIONS_PROVIDERS`
   dict — that is where the flat `AZURE_OPENAI_*` settings get folded into an `"azure"` entry,
   which sidesteps the concatenation problem entirely because the merge happens in Python at
   Django settings load rather than in YAML.

This repo delivers items the plugin needs (the settings in `18-azure-openai-secrets.yaml`);
items 1-3 are `mitodl/edx-extensions` work and remain out of scope per §9. The mit-learn and
learn-ai pieces have no such dependency and should ship first.

Mechanically: a new mitxonline-only `builder.create_dynamic(...)` call in
`src/ol_infrastructure/applications/edxapp/k8s_secrets.py`, a new field on the `EdxappSecrets` dataclass next to
`translations_providers`, and registration in the `lms_edxapp_config_sources` /
`cms_edxapp_config_sources` dicts in `src/ol_infrastructure/applications/edxapp/k8s_resources.py` so the file is actually mounted.
Policy grants go in `src/ol_infrastructure/applications/edxapp/edxapp_mitxonline_policy.hcl` only.

## 6. Delivery wiring (omitted from the plan, required)

1. `src/ol_infrastructure/lib/pulumi_projects.py` — add
   `VAULT_AZURE = "ol-substructure-vault-azure"` alongside the block at `:50-56`, and
   `VAULT_AZURE: "substructure.vault.azure"` in the path map at `:155-161`. Add the
   `src/ol_infrastructure/infrastructure/azure/openai` project to the same registry.
2. `src/ol_concourse/pipelines/infrastructure/vault/pipeline.py:69` — add `"azure"` to the
   `for substructure in [...]` list so the mount is deployed CI→QA→Production by the existing
   `packer-pulumi-vault` pipeline.
3. A pipeline for `src/ol_infrastructure/infrastructure/azure/openai`. It is not a Vault substructure and does not
   belong in the vault pipeline; add a new
   `src/ol_concourse/pipelines/infrastructure/azure/pipeline.py` using `pulumi_jobs_chain`,
   watching `src/ol_infrastructure/infrastructure/azure/`.
4. Concourse must be able to reach the SOPS-encrypted Azure creds — they decrypt via the same
   KMS/Vault-transit keys as every other `src/bridge/secrets/pulumi/*` file, so no new
   credential plumbing is expected, but confirm during the first CI run.

## 7. Open questions — answered 2026-08-10 except Q1

- **Q1 — Subscription & tenant. STILL OPEN.** Which Azure subscription and tenant? Is
  `Microsoft.CognitiveServices` registered on it, and is the subscription approved for Azure
  OpenAI? Is there regional TPM quota for the models in Q4? A subscription without quota
  will fail at `Deployment` creation, not at plan time. This blocks *deployment*, not the code:
  credentials are read from SOPS at run time, so both Pulumi projects can be written and
  reviewed first. See the note under Q5 — the account layout divides one regional TPM pool 27
  ways, so the quota answer needs to be a number, not a yes.
- **Q2 — Admin consent. ANSWERED: Tobias Macey holds tenant Global Admin and will grant
  consent directly.** No IS&T ticket needed. The three `az ad app permission admin-consent`
  invocations (one per environment) become a documented post-deploy step in the project
  README rather than a scheduling risk. This removes what was the project's largest stall risk.
- **Q3 — edxapp setting names. ANSWERED by reading the plugin source, with a finding that
  changes §5.3.** See §5.3 as revised. Two corrections came out of it: the config-source
  number `17-` is already taken by `17-webhook-tokens-secrets.yaml`, so the Azure file is
  `18-azure-openai-secrets.yaml`; and the translations plugin has no code path that reads flat
  `OPENAI_*` settings at all, so delivering the credentials is necessary but not sufficient.
- **Q4 — Model deployments. ANSWERED: `gpt-4o`, `gpt-4o-mini`, and `gpt-5.2`.** Covers
  learn-ai's current defaults and edxapp's translations model in one deployment set, so Azure
  is a drop-in for both consumers. All three need regional TPM quota (folds into Q1).
- **Q5 — Environment scope. ANSWERED: the account boundary follows the app consumer, and
  Production is separated from pre-production.** Concretely: one resource group per
  environment, holding one Cognitive Services account per consumer.

  ```
  ol-openai-production/  ol-openai-{mitlearn,learn-ai,mitxonline}-production
  ol-openai-qa/          ol-openai-{mitlearn,learn-ai,mitxonline}-qa
  ol-openai-ci/          ol-openai-{mitlearn,learn-ai,mitxonline}-ci
  ```

  One Vault root SP per environment, scoped to that environment's resource group — so three
  consent grants, not nine. Each Vault role is scoped to its own consumer's account, so
  mit-learn's dynamic credentials cannot reach learn-ai's endpoint.

  Two facts drove this over the alternatives. First, Azure Monitor's Cognitive Services
  metrics are dimensioned by model deployment name but carry **no stable caller dimension** —
  and Vault mints a fresh service principal every lease, so per-consumer attribution by
  calling identity is unworkable by construction. Attribution has to come from the resource.
  Second, TPM quota is allocated per subscription per region per model and is *shared* across
  accounts, so splitting into more accounts does not buy throughput isolation; per-deployment
  capacity allocation does, and that works in any layout.

  Consequence to watch: 9 accounts × 3 models = 27 deployments dividing one regional pool.
  Allocate capacity heavily to Production and minimally to CI/QA.
- **Q6 — Lease TTL. ANSWERED: `ttl="24h"` / `max_ttl="48h"`,** the component defaults. Azure AD
  service-principal creation is eventually consistent and fresh credentials are typically
  unusable for 10-60s, so rotation is deliberately infrequent. App-side retry on 401 is still
  required and remains the app repos' responsibility (§9).

## 8. Verification plan

- `pulumi preview` on every new/changed project in dependency order; **confirm zero diff on
  all existing OpenAI-related resources** (the additive guarantee).
- After the Azure infra deploy: `az cognitiveservices account show`, `az ad sp show`, and
  `az role assignment list --scope <rg-id>` confirm the account, deployments, root SP, and
  RG-scoped role assignment.
- After the substructure deploy: `vault read azure-openai/creds/ol-mitlearn-openai` (and the
  other two). A clean `client_id`/`client_secret` proves admin consent was actually granted; a
  missing-consent failure surfaces here as an explicit Azure AD error.
- End-to-end token exchange with the minted creds:
  `az login --service-principal -u <client_id> -p <client_secret> --tenant <tenant_id>`,
  `az account get-access-token --resource https://cognitiveservices.azure.com/`, then `curl`
  the deployed model's chat-completions endpoint with that bearer token.
- After the per-app deploys: `kubectl get vaultdynamicsecret -n <ns>` and `kubectl exec` to
  confirm `AZURE_OPENAI_*` are present **and** `OPENAI_API_KEY` is still present and unchanged.
- `pre-commit run --all-files` and `mypy` on all touched files.

## 9. Out of scope

Application source changes in mit-learn / learn-ai / edx-platform / edx-extensions to actually
use `azure-identity` + the Azure OpenAI SDK; automating tenant admin consent; rotating or
removing the existing static OpenAI keys; a shared AI gateway/proxy; Azure OpenAI cost/budget
alerting; multi-region failover.
