# Spec: Vault-managed Azure OpenAI credentials (mit-learn, learn-ai, edxapp)

Project: `wp-vault-managed-azure-openai-credentials-for-mit-l-cd2801`
Source plan: `~/.claude/plans/the-code-mit-apps-maintained-mit-learn-a-rosy-pinwheel.md`
Phase: spec — this document is the buildable specification derived from that plan,
with the plan's assumptions verified against the codebase and the gaps it left open
resolved or flagged.

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
| mit-learn `_create_dynamic_secret` helper + AWS example | **Confirmed** at `applications/mit_learn/k8s_secrets.py:79` and `:313-328` |
| learn-ai dynamic-secret + `OLVaultK8SResources` block | **Confirmed** at `applications/learn_ai/__main__.py:640-690` |
| edxapp `VaultSecretBuilder.create_static/create_dynamic` | **Confirmed** at `applications/edxapp/secrets_factory.py:77` (static) and `:121` (dynamic — returns a `Callable` for use with `Output.apply`) |
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
| **Pulumi deploy SP** | **Manually, once, out of band** (bootstrap) | `Contributor` + `User Access Administrator` (or `Owner`) on the target subscription — needs to create resource groups, Cognitive Services accounts, app registrations, and role assignments. Plus Graph `Application.ReadWrite.All` to create the Vault root app registration. | `pulumi_azure_native.Provider` / `pulumi_azuread.Provider` in `infrastructure/azure/openai` |
| **Vault root SP** | **By Pulumi** (`azuread.Application` + `ServicePrincipal` + `ApplicationPassword`) | Graph `Application.ReadWrite.OwnedBy` (application permission, **requires tenant admin consent**) + `User Access Administrator` scoped **only to the new resource group** | Vault's Azure secrets engine (`azure.Backend` `client_id`/`client_secret`) |
| **Per-app dynamic SPs** | **By Vault at request time** | `Cognitive Services OpenAI User` scoped to the Cognitive Services account | mit-learn / learn-ai / edxapp pods |

**Bootstrap credential storage.** This repo's established pattern for non-AWS provider
credentials is SOPS-encrypted YAML read via `bridge.secrets.sops.read_yaml_secrets`
(see `infrastructure/qdrant_cloud/__main__.py:17` and `infrastructure/mongodb_atlas/__main__.py:64`).
Follow it: new files `src/bridge/secrets/pulumi/azure.{ci,qa,production}.yaml` holding
`tenant_id`, `subscription_id`, `client_id`, `client_secret` for the **Pulumi deploy SP**.
`.sops.yaml`'s existing `path_regex` rules already cover `*.ci.yaml` / `*.qa.yaml` /
`*.production.yaml`, so no `.sops.yaml` change is needed.

## 4. Resource specification

### 4.1 `src/ol_infrastructure/infrastructure/azure/openai/`

New standalone Pulumi project. Layout mirrors `infrastructure/qdrant_cloud/`:
`Pulumi.yaml` (name `ol-infrastructure-azure-openai`, backend `s3://mitol-pulumi-state/`),
`Pulumi.{CI,QA,Production}.yaml`, `__init__.py`, `__main__.py`, `README.md`.

Provisions, per environment:

- `azure_native.resources.ResourceGroup` — `ol-openai-{env}`
- `azure_native.cognitiveservices.Account` — `kind="OpenAI"`, `sku="S0"`,
  `custom_sub_domain_name` set (required for AAD token auth — without it only key auth works),
  `public_network_access="Enabled"`, `disable_local_auth` left **false** for now (additive
  migration; key auth stays available)
- `azure_native.cognitiveservices.Deployment` per model — see §7 open question Q4
- `azuread.Application` + `ServicePrincipal` + `ApplicationPassword` for the Vault root SP,
  requesting Microsoft Graph `Application.ReadWrite.OwnedBy` (app permission)
- `azure_native.authorization.RoleAssignment` — Vault root SP → `User Access Administrator`,
  `scope = resource_group.id` (**not** subscription scope)

Stack outputs (consumed by the substructure project via `StackReference`):
`resource_group_id`, `cognitive_account_id`, `cognitive_account_endpoint`,
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
line at `vault.py:21`.

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
`substructure/vault/*` project).

`__main__.py`:
- `StackReference("…/ol-infrastructure-azure-openai/{env}")`
- instantiate `OLVaultAzureSecretsEngine` at mount `azure-openai` with three roles:
  `ol-mitlearn-openai`, `ol-learn-ai-openai`, `ol-mitxonline-openai`, each
  `azure_roles=[{role_name: "Cognitive Services OpenAI User", scope: cognitive_account_id}]`
- export `azure_openai_mount_path`, `cognitive_account_endpoint`, `model_deployments`

## 5. Per-app wiring (additive)

### 5.1 mit-learn

- `applications/mit_learn/mitlearn_policy.hcl`: add
  `azure-openai/creds/ol-mitlearn-openai` and `.../ *` read grants; extend both
  `sys/leases/renew` and `sys/leases/revoke` `allowed_parameters.lease_id` lists with
  `azure-openai/creds/ol-mitlearn-openai/*` (VSO cannot renew/revoke otherwise).
- `applications/mit_learn/k8s_secrets.py`: one new `_create_dynamic_secret(...)` call —
  `mount="azure-openai"`, `path="creds/ol-mitlearn-openai"`, templates
  `AZURE_OPENAI_CLIENT_ID` ← `client_id`, `AZURE_OPENAI_CLIENT_SECRET` ← `client_secret`.
  Append to `secret_names` / `secret_resources` like the AWS block at `:313`.
- `applications/mit_learn/__main__.py`: plain (non-secret) container env vars
  `AZURE_OPENAI_ENDPOINT`, `AZURE_OPENAI_TENANT_ID`, `AZURE_OPENAI_API_VERSION`.

### 5.2 learn-ai

Same three pieces, in `applications/learn_ai/learn_ai_policy.hcl` (role
`ol-learn-ai-openai`) and a new `OLVaultK8SSecret`/`OLVaultK8SDynamicSecretConfig` block in
`applications/learn_ai/__main__.py` modelled on the db-creds block at `:657-690`. Include
`restart_target_kind="Deployment"` / `restart_target_name="learn-ai-app"` so credential
rotation restarts the app, matching the db-creds secret.

`AI_DEFAULT_*_MODEL` values (`openai/gpt-4o-mini`, `openai/gpt-4o` in all three stack
YAMLs) are **not** changed in this pass.

### 5.3 edxapp / mitxonline — blocked on a contract decision

**Finding that changes the plan:** edxapp does not deep-merge its config sources. The init
container runs `cat /openedx/config-sources/*/*.yaml > /openedx/config/lms.env.yml`
(`applications/edxapp/k8s_resources.py:639`, `:950`, and the CronJob variants at `:1202`,
`:1322`, `:1437`, `:1539`). Files are **concatenated into one YAML document**.

Consequence: the existing `TRANSLATIONS_PROVIDERS:` mapping is emitted by the *static*
`14-translations-providers-secrets.yaml` secret (`k8s_secrets.py:386-411`). Azure credentials
come from a *different Vault mount* and therefore a *different* VSO secret and file. A second
file also emitting a top-level `TRANSLATIONS_PROVIDERS:` key would produce a duplicate key —
last-one-wins, silently clobbering the deepl/openai/gemini/mistral providers.

Therefore the Azure creds for edxapp **must** be delivered as new, distinct top-level settings
in their own file. Existing numbers are `10-general`, `11-xqueue`, `12-forum`,
`13-canvas-syllabus-token`, `14-translations-providers`, `15-meilisearch`, `16-typesense`, so
use **`17-azure-openai-secrets.yaml`**:

```yaml
AZURE_OPENAI_CLIENT_ID: ...
AZURE_OPENAI_CLIENT_SECRET: ...
AZURE_OPENAI_ENDPOINT: ...
AZURE_OPENAI_TENANT_ID: ...
```

and the edx-extensions plugin must read them from there. **That setting-name contract has to
be agreed with the plugin authors before this piece is implemented** (Q3 below). The mit-learn
and learn-ai pieces have no such dependency and can ship first.

Mechanically: a new mitxonline-only `builder.create_dynamic(...)` call in
`applications/edxapp/k8s_secrets.py`, a new field on the `EdxappSecrets` dataclass next to
`translations_providers`, and registration in the `lms_edxapp_config_sources` /
`cms_edxapp_config_sources` dicts in `k8s_resources.py` so the file is actually mounted.
Policy grants go in `applications/edxapp/edxapp_mitxonline_policy.hcl` only.

## 6. Delivery wiring (omitted from the plan, required)

1. `src/ol_infrastructure/lib/pulumi_projects.py` — add
   `VAULT_AZURE = "ol-substructure-vault-azure"` alongside the block at `:50-56`, and
   `VAULT_AZURE: "substructure.vault.azure"` in the path map at `:155-161`. Add the
   `infrastructure/azure/openai` project to the same registry.
2. `src/ol_concourse/pipelines/infrastructure/vault/pipeline.py:69` — add `"azure"` to the
   `for substructure in [...]` list so the mount is deployed CI→QA→Production by the existing
   `packer-pulumi-vault` pipeline.
3. A pipeline for `infrastructure/azure/openai`. It is not a Vault substructure and does not
   belong in the vault pipeline; add a new
   `src/ol_concourse/pipelines/infrastructure/azure/pipeline.py` using `pulumi_jobs_chain`,
   watching `src/ol_infrastructure/infrastructure/azure/`.
4. Concourse must be able to reach the SOPS-encrypted Azure creds — they decrypt via the same
   KMS/Vault-transit keys as every other `src/bridge/secrets/pulumi/*` file, so no new
   credential plumbing is expected, but confirm during the first CI run.

## 7. Open questions (must be answered before implementation starts)

- **Q1 — Subscription & tenant.** Which Azure subscription and tenant? Is
  `Microsoft.CognitiveServices` registered on it, and is the subscription approved for Azure
  OpenAI? Is there regional TPM quota for the models in §7/Q4? A subscription without quota
  will fail at `Deployment` creation, not at plan time.
- **Q2 — Admin consent.** Who on the team has (or can get) tenant Global Admin to grant
  `Application.ReadWrite.OwnedBy` consent? Without it, every step through §4.2 applies cleanly
  and then `vault read azure-openai/creds/...` fails. This is the single most likely thing to
  stall the project.
- **Q3 — edxapp setting names.** What top-level settings will the edx-extensions plugin read
  (see §5.3)? Needed before the edxapp piece can be written.
- **Q4 — Model deployments.** The plan says `gpt-4o` / `gpt-4o-mini` (learn-ai's current
  defaults). But edxapp's translations config already specifies `gpt-5.2` for OpenAI
  (`k8s_secrets.py:404`). Confirm the deployment list — deploying only 4o-family models makes
  Azure unusable as a drop-in for edxapp translations.
- **Q5 — Environment scope.** One Azure OpenAI account per environment (CI/QA/Production), as
  the plan assumes? That is 3 accounts and 3 admin-consent grants. Alternative: one Production
  account with per-env resource groups and roles. Recommend per-env for blast-radius isolation
  despite the extra consent grants.
- **Q6 — Lease TTL.** Azure AD service-principal creation is eventually consistent; freshly
  minted credentials are typically unusable for 10-60s. Recommend `ttl="24h"` / `max_ttl="48h"`
  (not minutes) so rotation is infrequent, plus app-side retry on 401. Confirm this is
  acceptable versus the security posture the team wants.

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
