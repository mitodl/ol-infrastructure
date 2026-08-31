# Spec: Azure OpenAI credentials for mit-learn, learn-ai, edxapp

Tracked as workflow project `wp-vault-managed-azure-openai-credentials-for-mit-l-cd2801`,
where the implementation task breakdown and dependency graph live.

**This document is the source of truth.** All file references are repo-relative.

**Decided 2026-08-31: workloads authenticate to Azure OpenAI with workload identity
federation against user-assigned managed identities. There is no Azure credential to store,
rotate, or revoke anywhere in this design.** The previously specified approach, Vault's Azure
secrets engine minting short-lived service principals, is recorded as a rejected alternative
in §9 along with the evidence that killed it. Commits `9667cc84d`, `b286ef089`, `cf1cdfe80`,
`ed74de485`, and `15a82bd6d` on this branch implement the rejected design and are superseded.

## 1. Goal

Give three consumers (mit-learn, learn-ai, edxapp/mitxonline) Entra-authenticated access to
Azure OpenAI **alongside** their existing static `OPENAI_API_KEY` wiring, which is not
touched. This repo provisions the Azure resources and delivers the identity configuration
into the pod environment. Making the applications actually use it is separate work in each
app repo (§10).

## 2. The design in one paragraph

Each consumer gets a user-assigned managed identity per environment. That identity trusts the
environment's EKS cluster OIDC issuer for exactly one Kubernetes ServiceAccount subject, and
holds `Cognitive Services OpenAI User` on exactly one Cognitive Services account. Pods project
a ServiceAccount token with the `api://AzureADTokenExchange` audience; `azure-identity`
exchanges it for an Entra access token with no secret involved. Developers on laptops get the
same access under their own MIT Entra identity via `az login`, scoped to CI accounts only.
`DefaultAzureCredential` covers both without an environment branch in application code.

## 3. Identity model

| Identity | Created by | Permissions | Used by |
| --- | --- | --- | --- |
| **Pulumi deploy SP** (`ol-pulumi-azure-openai-deploy`, appId `9c89da33-dc56-4a07-ae42-1bcdddb82798`) | Manually, once, out of band. Already exists. | `Contributor` + `User Access Administrator` on the target subscription (or `Owner`). **No Microsoft Graph permission and no Entra directory role.** | `pulumi_azure_native.Provider` in `src/ol_infrastructure/infrastructure/azure/openai` |
| **Per-consumer user-assigned managed identity**, one per consumer per environment | Pulumi, as an ARM resource | `Cognitive Services OpenAI User` (role id `5e0bd9bd-7b93-4f28-af87-19fc36ad61bd`) scoped to **its own consumer's** Cognitive Services account | The consumer's pods, via `WorkloadIdentityCredential` |
| **Developer identities** | Already exist in the MIT tenant | `Cognitive Services OpenAI User` on the **CI** accounts only, granted to one Entra security group | Local development, via `AzureCliCredential` after `az login` |

### 3.1 Why managed identities and not app registrations

This is the load-bearing choice, and it is what makes the design deployable with the access we
actually hold.

An app registration is an Entra directory object. A service principal that creates one needs
Microsoft Graph `Application.ReadWrite.All` or `Application.ReadWrite.OwnedBy`. Both are
Microsoft Graph app roles, and consenting to a Microsoft Graph app role requires the
Privileged Role Administrator directory role, which we do not have and cannot self-grant
(§9.1). Putting app registrations in the design reintroduces the exact blocker that killed the
Vault approach, one step removed.

A user-assigned managed identity is an ARM resource under the subscription. Creating it,
attaching federated identity credentials to it, and assigning it an Azure RBAC role are all
subscription-scope operations already covered by `Contributor` + `User Access Administrator`.
**This design performs zero Entra directory writes**, so it needs no admin consent, no
directory role, and no IS&T ticket.

Secondary benefit: the Graph `Application.ReadWrite.All` grant previously specified for the
Pulumi deploy SP is no longer needed at all, which meaningfully shrinks the blast radius of
that bootstrap credential.

### 3.2 Bootstrap credential storage

Unchanged from the earlier plan. SOPS-encrypted YAML read via
`bridge.secrets.sops.read_yaml_secrets`, following
`src/ol_infrastructure/infrastructure/qdrant_cloud/__main__.py:17` and
`src/ol_infrastructure/infrastructure/mongodb_atlas/__main__.py:64`. Files
`src/bridge/secrets/pulumi/azure.{ci,qa,production}.yaml` holding `tenant_id`,
`subscription_id`, `client_id`, `client_secret` for the deploy SP. `.sops.yaml`'s existing
`path_regex` rules already cover `*.ci.yaml` / `*.qa.yaml` / `*.production.yaml`, so no
`.sops.yaml` change is needed.

## 4. Resource specification: `src/ol_infrastructure/infrastructure/azure/openai/`

One standalone Pulumi project, layout mirroring
`src/ol_infrastructure/infrastructure/qdrant_cloud/`: `Pulumi.yaml` (name
`ol-infrastructure-azure-openai`, backend `s3://mitol-pulumi-state/`),
`Pulumi.{CI,QA,Production}.yaml`, `__init__.py`, `__main__.py`, `README.md`.

Per environment, for `consumer in {mitlearn, learn-ai, mitxonline}`:

- `azure_native.resources.ResourceGroup`, `ol-openai-{env}`, one per environment.
- `azure_native.cognitiveservices.Account`, one per consumer, named
  `ol-openai-{consumer}-{env}`. `kind="OpenAI"`, `sku="S0"`, `public_network_access="Enabled"`,
  `disable_local_auth` left **false** (additive migration, key auth stays available).
  `custom_sub_domain_name` **must** be set or the account accepts only API-key auth, which
  defeats Entra token auth entirely.
- `azure_native.cognitiveservices.Deployment` for `gpt-4o`, `gpt-4o-mini`, `gpt-5.2` on each
  account. Model versions stay unpinned by default (which versions exist is a property of the
  subscription and region, and a wrong version string fails at deployment creation rather than
  at preview); pin via a `{model: version}` stack config map once confirmed, which also
  switches the deployment to `NoAutoUpgrade`.
- `azure_native.managedidentity.UserAssignedIdentity`, one per consumer,
  `ol-openai-{consumer}-{env}`.
- `azure_native.managedidentity.FederatedIdentityCredential` on that identity. See §4.1.
- `azure_native.authorization.RoleAssignment`: identity to `Cognitive Services OpenAI User`,
  `scope = <that consumer's account>.id`. Account scope, **not** resource-group scope: this
  scoping is what stops one app's identity from reaching another app's endpoint. Set
  `principal_type="ServicePrincipal"` explicitly, otherwise a freshly created identity that
  has not yet replicated fails the assignment with `PrincipalNotFound`.

Stack outputs: `resource_group_id`, `cognitive_accounts` (consumer to `{id, endpoint}`),
`model_deployments`, `workload_identities` (consumer to `{client_id, principal_id}`),
`tenant_id`.

### 4.1 Federated identity credential

One per consumer per environment. Every field is an exact string match with no wildcard
support anywhere, and a wrong value creates successfully and fails only later at token
exchange, so generate all of them from the same Pulumi values that build the ServiceAccount.

- `issuer`: the environment's EKS cluster OIDC issuer URL. Available by `StackReference` on
  the cluster stack, which exports `cluster_identities`
  (`src/ol_infrastructure/infrastructure/aws/eks/__main__.py:431`); the issuer is
  `cluster_identities[0]["oidcs"][0]["issuer"]`, indexed exactly as
  `src/ol_infrastructure/components/aws/eks.py:381-382` already does for IRSA trust policies.
- `subject`: `system:serviceaccount:{namespace}:{serviceaccount}`.
- `audiences`: exactly `["api://AzureADTokenExchange"]`. Azure rejects any other count with
  `400 Federated identity credentials must have exactly one audience`.

The ServiceAccounts, confirmed against `pulumi preview`:

| Consumer | Namespace | ServiceAccount |
| --- | --- | --- |
| edxapp / mitxonline | `mitxonline-openedx` | `mitxonline-edxapp-vault` (`applications/edxapp/k8s_resources.py:252`) |
| learn-ai | `learn-ai` | `learn-ai-admin` (`applications/learn_ai/__main__.py:254`) |
| mit-learn | `mitlearn` | `mitlearn-app` — **created by this work**, see below |

edxapp is the case worth calling out: every Deployment and every CronJob variant runs under
that one ServiceAccount, so one federated credential covers the whole deployment including
migrations and periodic jobs. Nine identities with one credential each, against a documented
cap of 20 credentials per identity, leaves the cap irrelevant here.

**Correction, found during implementation.** This table previously recorded mit-learn's
ServiceAccount as "the SA created alongside its `OLVaultK8SResources` block". That is the VSO
*sync* ServiceAccount (`mitlearn-vault`), not the one the pods run under.
`applications/mit_learn/__main__.py` never set `application_service_account_name`, so every
mit-learn Deployment ran under the `mitlearn` namespace's `default` ServiceAccount. A
federated credential subject is an exact string with no wildcards, so federating to `default`
would trust anything that ever runs in that namespace. The implementation therefore creates a
dedicated `mitlearn-app` ServiceAccount and points the workloads at it. `pulumi preview` on
`ol-application-mit-learn/CI` shows exactly this and nothing else: one ServiceAccount created,
`serviceAccountName: mitlearn-app` added to five Deployments (webapp, three celery workers,
beat), 148 resources unchanged. Those five roll once on the first deploy.

Two Azure behaviours to design around:

1. **Propagation delay.** A token request made within a few minutes of creating a federated
   credential can fail with `AADSTS70021: No matching federated identity record found for
   presented assertion`. Microsoft recommends retry logic on every token request, not just the
   first. This is the same class of eventual consistency the rejected design needed for freshly
   minted service principals, so it is not new burden, but it is still app-side work (§10).
2. **Concurrent writes.** Creating multiple federated credentials under the same identity
   concurrently returns `409 Conflict`. At one credential per identity this cannot bite; if a
   consumer ever needs a second, chain them with `depends_on`.

## 5. Per-app wiring

No Vault secret, no VSO object, and no Vault policy change for any consumer. Everything below
is non-secret configuration.

### 5.1 Pod specification, all three consumers

Each workload needs a projected ServiceAccount token and four environment variables:

```yaml
volumes:
  - name: azure-identity-token
    projected:
      sources:
        - serviceAccountToken:
            path: azure-identity-token
            expirationSeconds: 3600
            audience: api://AzureADTokenExchange
volumeMounts:
  - name: azure-identity-token
    mountPath: /var/run/secrets/azure/tokens
    readOnly: true
env:
  AZURE_CLIENT_ID: <the consumer's managed identity client_id>
  AZURE_TENANT_ID: <tenant id>
  AZURE_FEDERATED_TOKEN_FILE: /var/run/secrets/azure/tokens/azure-identity-token
  AZURE_AUTHORITY_HOST: https://login.microsoftonline.com/
```

The audience on the projected token is deliberately not IRSA's `sts.amazonaws.com`. The two
are independent; adding this volume does not disturb existing IRSA behaviour on the same
ServiceAccount.

**No mutating webhook.** On AKS the `azure-workload-identity` webhook injects the block above.
We write it directly instead: it is roughly ten lines in pod specs this repo already generates,
and it is not worth a cluster-wide webhook, a new Helm release, and another upgrade surface to
avoid them.

### 5.2 mit-learn and learn-ai

Plain container environment variables in
`src/ol_infrastructure/applications/mit_learn/__main__.py` and
`src/ol_infrastructure/applications/learn_ai/__main__.py`: the four from §5.1 plus
`AZURE_OPENAI_ENDPOINT`, `AZURE_OPENAI_API_VERSION`, and `AZURE_OPENAI_DEFAULT_DEPLOYMENT`,
read from a `StackReference` on the Azure project. `AI_DEFAULT_*_MODEL` values are not changed
in this pass.

### 5.3 edxapp / mitxonline

The config-source concatenation constraint still applies even though nothing here is secret.
The init container runs `cat /openedx/config-sources/*/*.yaml > /openedx/config/lms.env.yml`
(`src/ol_infrastructure/applications/edxapp/k8s_resources.py:639`, `:950`, and the CronJob
variants at `:1202`, `:1322`, `:1437`, `:1539`). Files are concatenated into one YAML
document, not deep-merged, so two sources emitting the same top-level key produce a duplicate
that silently resolves last-one-wins.

`TRANSLATIONS_PROVIDERS:` is emitted by the static `14-translations-providers-secrets.yaml`
(`src/ol_infrastructure/applications/edxapp/k8s_secrets.py:386-411`). A second file emitting
that key would wipe out the deepl/openai/gemini/mistral providers with no error anywhere.
Azure settings therefore ship as distinct flat top-level keys in their own numbered file.
`17-` is taken by `17-webhook-tokens-secrets.yaml`, so this is
**`18-azure-openai-config.yaml`** (config, not `-secrets`, because it now holds no secret):

```yaml
AZURE_OPENAI_CLIENT_ID: ...
AZURE_OPENAI_TENANT_ID: ...
AZURE_OPENAI_ENDPOINT: ...
AZURE_OPENAI_API_VERSION: ...
AZURE_OPENAI_DEFAULT_DEPLOYMENT: ...
```

Flat `SCREAMING_SNAKE` top-level keys match every other config source in that directory
(`COMMENTS_SERVICE_KEY`, `MEILISEARCH_MASTER_KEY`, `TYPESENSE_API_KEY`,
`CERTIFICATE_WEBHOOK_ACCESS_TOKEN`). Mechanically this is a ConfigMap-backed source rather
than a VSO secret, registered in the `lms_edxapp_config_sources` / `cms_edxapp_config_sources`
dicts in `k8s_resources.py`, mitxonline only.

**Delivering these settings is necessary but not sufficient.** The translations plugins read
provider credentials from exactly one place,
`settings.TRANSLATIONS_PROVIDERS[provider_name]["api_key"]` in
`ol_openedx_course_translations/utils/course_translations.py:64-76`, dispatching to one of
three hardcoded classes (`OpenAIProvider`, `GeminiProvider`, `MistralProvider` in
`providers/llm_providers.py`). No code path reads flat `OPENAI_*` settings and no Azure
provider exists. The plugin work in `mitodl/edx-extensions` is:

1. a new `AzureOpenAIProvider(LLMProvider)`. The base class builds LiteLLM model strings as
   `f"openai/{model}"`, so Azure needs `f"azure/{deployment_name}"`;
2. `_call_llm` passes only `api_key=`, but Azure needs `api_base`, `api_version`, and a bearer
   token. Under this design the token comes from `DefaultAzureCredential` (which picks up
   `WorkloadIdentityCredential` in-cluster with no configuration) passed as LiteLLM's
   `azure_ad_token`. Note this differs from the rejected design, where the plugin would have
   had to build a `ClientSecretCredential` from a Vault-issued client id and secret;
3. `apply_common_settings` (`settings/common.py:39`) folds the flat `AZURE_OPENAI_*` settings
   into an `"azure"` entry of `TRANSLATIONS_PROVIDERS`. Doing the merge in Python at Django
   settings load is what sidesteps the YAML concatenation problem.

mit-learn and learn-ai have no equivalent dependency and should ship first.

## 6. Local development

**Default: developers authenticate as themselves.** One Entra security group holding the
engineers who need it, granted `Cognitive Services OpenAI User` on the three **CI**
Cognitive Services accounts only. Nothing on QA or Production. A developer runs `az login`
once; `DefaultAzureCredential` finds `AzureCliCredential` and the same application code that
runs in the cluster works on the laptop.

This works because `DefaultAzureCredential` is a chain, attempted in this order in Python:
1 Environment, 2 **Workload Identity**, 3 Managed Identity, 4 Shared Token Cache, 5 VS Code,
6 **Azure CLI**, 7 Azure PowerShell, 8 Azure Developer CLI. Position 2 fires in the cluster,
position 6 fires on a laptop. No environment branch in application code, and no Azure
credential on any developer machine.

Both prerequisites are already satisfied in the tenant: `allowedToCreateSecurityGroups` is
`true`, and assigning an Azure RBAC role to that group is a subscription-scope operation
covered by the `Owner` grant we hold on `1a5054d4-e995-477d-9551-e08d33f60fdb`.

Fallback for anyone without an MIT Entra account, such as outside contributors on
edx-extensions: the existing static `OPENAI_API_KEY` path, which this migration deliberately
leaves intact. Their local runs exercise OpenAI directly rather than Azure. A shared
long-lived client secret for local development was considered and rejected; it reintroduces
exactly the credential this design exists to eliminate.

## 7. Delivery wiring

1. `src/ol_infrastructure/lib/pulumi_projects.py`: register
   `src/ol_infrastructure/infrastructure/azure/openai`. The `VAULT_AZURE` /
   `ol-substructure-vault-azure` entry from the rejected design is **not** added.
2. `src/ol_concourse/pipelines/infrastructure/azure/pipeline.py`, using `pulumi_jobs_chain`,
   watching `src/ol_infrastructure/infrastructure/azure/`. CI to QA to Production.
3. No change to `src/ol_concourse/pipelines/infrastructure/vault/pipeline.py`. The rejected
   design added `"azure"` to the substructure list at `:69`; that must be reverted.
4. Concourse decrypts `src/bridge/secrets/pulumi/azure.*.yaml` with the same KMS/Vault-transit
   keys as every other file in that directory, so no new credential plumbing is expected.
   Confirm on the first CI run.
5. The Azure project takes a `StackReference` on the per-environment EKS cluster stack for the
   OIDC issuer, which makes it a downstream of that stack in the pipeline ordering.

## 8. Verification plan

- `pulumi preview` on every new or changed project in dependency order, confirming **zero
  diff on all existing OpenAI-related resources** (the additive guarantee).
  - Done for the app stacks with the feature flag off, which is how they merge:
    `ol-application-learn-ai/CI` 110 unchanged and `ol-application-edxapp/mitxonline.CI` 231
    unchanged, both zero diff. `ol-application-mit-learn/CI` is the one exception and it is
    the intended one, see the correction in §4.1.
  - Not yet possible for `ol-infrastructure-azure-openai` itself: its provider reads
    `src/bridge/secrets/pulumi/azure.{env}.yaml`, which the bootstrap task has not created
    yet, so the program cannot construct a provider to preview against.
- After the Azure deploy: `az cognitiveservices account show`, `az identity show`,
  `az identity federated-credential list --identity-name <id> -g <rg>`, and
  `az role assignment list --scope <account-id>` confirm the account, deployments, identity,
  federated credential, and account-scoped role assignment.
- Token exchange end to end, from inside a pod, which is the only place the federation can be
  exercised:
  ```shell
  kubectl exec -n <ns> deploy/<app> -- python -c "
  from azure.identity import DefaultAzureCredential
  print(DefaultAzureCredential().get_token('https://cognitiveservices.azure.com/.default').expires_on)"
  ```
  Then a chat-completions call against the deployed model with that bearer token. Allow for
  the propagation delay in §4.1 on a freshly created credential; a failure inside the first few
  minutes is not conclusive.
- Local path: `az login`, then the same two-line snippet on a laptop, which must succeed
  against a **CI** endpoint and fail against Production.
- After the per-app deploys: `kubectl exec` to confirm the four `AZURE_*` variables and the
  projected token file are present, **and** that `OPENAI_API_KEY` is still present and
  unchanged.
- `pre-commit run --all-files` and `mypy` on all touched files.

## 9. Rejected alternative: Vault's Azure secrets engine

The original design mounted Vault's Azure secrets engine against a root service principal per
environment, minting a fresh Azure AD service principal per lease with 24h/48h TTLs, delivered
to pods as `AZURE_OPENAI_CLIENT_ID` / `AZURE_OPENAI_CLIENT_SECRET` through VSO dynamic secrets.

### 9.1 Why it was rejected

It cannot be deployed with the access we hold, and getting that access requires the most
privileged directory role in MIT's central tenant.

The Vault root SP needs Microsoft Graph `Application.ReadWrite.OwnedBy` (app role id
`18a4783c-866b-4cc7-a460-3d5e5662c884`) to create and delete service principals. Granting a
Microsoft Graph **app role** requires **Privileged Role Administrator**; Microsoft's
prerequisites explicitly exclude Cloud Application Administrator, AI Administrator, and
Application Administrator from consenting to Microsoft Graph app roles. Measured 2026-08-31 in
tenant `64afd9ba-0ecf-4acf-bc36-935f6235ba8b` (`mitprod.onmicrosoft.com`):

- The portal consent attempt returns `Authorization_RequestDenied`, "This operation can only
  be performed by an administrator."
- `roleManagement/directory/roleAssignments?$filter=principalId eq '<user>'` returns **zero**
  results. We hold no Entra directory role at all.
- `az role assignment list` shows `Owner` and `Billing Reader` on subscription
  `1a5054d4-e995-477d-9551-e08d33f60fdb`. That is Azure resource RBAC; consent is a directory
  operation, which is a different system.

**This supersedes the Q2 answer previously recorded in this document**, which asserted that
tenant Global Admin was held in-house and no IS&T ticket was needed. That was wrong.

Unblocking it would mean asking IS&T for one of: Privileged Role Administrator, a custom
directory role carrying
`microsoft.directory/servicePrincipals/managePermissionGrantsForAll.{policy}` scoped by an app
consent policy (Entra ID P1, creatable only via Graph PowerShell), or a one-off consent per
environment performed by an IS&T admin, which recurs on every new environment and every
re-creation of the root app registration.

### 9.2 What the chosen design also avoids

Even with consent granted, the Vault path carried costs that workload identity federation does
not:

- Vault creating and destroying service principals in MIT's central tenant on every lease.
- A root credential with directory-write authority, stored in Vault, that must itself be
  rotated.
- `rolloutRestartTargets` on every consuming workload, because the credential is read once at
  process start and revoked at max TTL. This was a real bug found in review on this branch and
  fixed in `15a82bd6d`; it is also a repo-wide latent problem tracked separately in
  `tk-vault-dynamic-secrets-are-missing-rolloutrestart-fd07b8`. Under federation nothing
  rotates, so the failure mode does not exist.
- Worse attribution, not better. Azure Monitor's Cognitive Services metrics carry no stable
  caller dimension, and a per-lease service principal changes object id every TTL, so usage
  could never be grouped by calling identity. A managed identity has a stable `client_id` that
  appears in Entra sign-in logs.

### 9.3 What stays true regardless

The account layout is unchanged, and the reasoning behind it survives the redesign intact.
One resource group per environment holding one Cognitive Services account per consumer:

```
ol-openai-production/  ol-openai-{mitlearn,learn-ai,mitxonline}-production
ol-openai-qa/          ol-openai-{mitlearn,learn-ai,mitxonline}-qa
ol-openai-ci/          ol-openai-{mitlearn,learn-ai,mitxonline}-ci
```

Attribution has to come from the resource, because the metrics have no caller dimension. And
TPM quota is *shared* across accounts, so splitting into more accounts does not buy throughput
isolation; per-deployment capacity allocation does, and that works in any layout.

The consequence to watch, corrected 2026-08-31 after a Copilot review caught the earlier
version of this paragraph asserting one pool for all 27 deployments: quota is pooled **per
model and version, per deployment type**. These are `GlobalStandard` deployments, and
[Microsoft's docs](https://learn.microsoft.com/en-us/azure/ai-foundry/openai/quotas-limits)
state that "deployments of the same model and version share one quota pool across all regions
in a subscription" — so the pool is not per region either, which the earlier text also got
wrong. Nine deployments of a given model (three consumers by three environments) share that
model's pool; the three models do not compete with each other. Plan capacity per model, and
allocate heavily to Production and minimally to CI and QA.

## 10. Open questions

- **Q1. Subscription and region. STILL OPEN, blocks deployment.** The subscription is
  `1a5054d4-e995-477d-9551-e08d33f60fdb` (`ol-engineering`). Confirm
  `Microsoft.CognitiveServices` is registered, the subscription is approved for Azure OpenAI,
  and the TPM quota for `gpt-4o`, `gpt-4o-mini`, and `gpt-5.2`. The answer needs to be **three
  numbers, one per model**, not a yes: each model has its own pool, and nine deployments share
  it (§4). A subscription without quota fails at `Deployment` creation, not at preview. The
  Production stack now refuses to preview at all without an explicit
  `azure_openai:model_capacity`, so this cannot be deployed on a guessed number. Blocks
  deploying, not writing or reviewing the code.
- **Q2. Entra security group for developers.** Which group, and who administers membership?
  Creating one is permitted (`allowedToCreateSecurityGroups: true`), but an existing
  IS&T-managed group with the right membership would be preferable to a new one nobody owns.
- **Q3. Retry policy for `AADSTS70021`.** Each app repo needs token-request retry. Confirm
  whether the shared settings/util layer in mit-learn and learn-ai is the right home, or
  whether each call site handles it.

## 11. Out of scope

Application source changes in mit-learn, learn-ai, edx-platform, and edx-extensions to
actually use `azure-identity` and the Azure OpenAI SDK; rotating or removing the existing
static OpenAI keys; a shared AI gateway or proxy; Azure OpenAI cost and budget alerting;
multi-region failover; `disable_local_auth` on the Cognitive Services accounts (a follow-up
once every consumer is off key auth).
