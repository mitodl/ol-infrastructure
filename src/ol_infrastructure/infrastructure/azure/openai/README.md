# Azure OpenAI infrastructure

Provisions the Azure side of the Azure OpenAI credentials work. The full design is in
`docs/plans/azure-openai-credentials-spec.md`.

## What this creates, per environment

One resource group (`ol-openai-{env}`) holding, per app consumer, one Cognitive
Services account and one user-assigned managed identity:

| Consumer | Account and identity | Federated subject |
| --- | --- | --- |
| mit-learn | `ol-openai-mitlearn-{env}` | `system:serviceaccount:mitlearn:mitlearn-app` |
| learn-ai | `ol-openai-learn-ai-{env}` | `system:serviceaccount:learn-ai:learn-ai-admin` |
| edxapp / mitxonline | `ol-openai-mitxonline-{env}` | `system:serviceaccount:mitxonline-openedx:mitxonline-edxapp-vault` |
| Dagster `ml` code location | `ol-openai-dagster-ml-{env}` | `system:serviceaccount:dagster:dagster-user-code` |

Each account gets a deployment of `gpt-4o`, `gpt-5-mini`, `gpt-5.2`, `gpt-5.4`,
`gpt-5.4-mini`, `gpt-5.4-nano`, and `gpt-5.4-pro`. `gpt-5-mini` stands in for
`gpt-4o-mini`, which Azure no longer accepts new deployments of. Each deployment is named
after its model, so the model name an app sends is the deployment name. An app can only
call models deployed on its own account, so this list is the set of models an app can
reach through Azure. Whether a model is allowed, deprecated, or retired, and what it
costs, is recorded separately in the `ol-llm` catalog
(`docs/plans/llm-usage-policies-spec.md` §2.2). See [Changing the deployed
models](#changing-the-deployed-models).

mit-learn's account also gets `text-embedding-3-large` and `text-embedding-3-small`
(`azure_openai:consumer_extra_models`). These have to be the same models mit-learn
already embeds with through OpenAI, because Qdrant only holds vectors from one model and
a different model would mean a full reindex. In QA and Production the Dagster `ml`
account also gets `text-embedding-3-large`, for the embeddings it computes of Learn's
content, which have to be comparable with Learn's vectors for the same reason.

learn-ai also gets a second account, `ol-decision-learn-ai-{env}`, of kind `AIServices`,
holding a deployment of `Microsoft-Decision-1.1` (`azure_openai:consumer_decision_models`).
Azure offers that model only on `AIServices` and `MaaS` accounts, with format
`Microsoft`, and the accounts above are kind `OpenAI`. Changing an account's kind
replaces it, which would take down the GPT deployments on it, so the decision model gets
its own account instead. learn-ai's existing identity holds `Cognitive Services User` on
it (OpenAI User's data actions are limited to `accounts/OpenAI/*`). Every decision model
must have an `azure_openai:model_versions` entry in every stack, CI and QA included, and
the stack fails at preview without one: whether Azure accepts a `Microsoft`-format
deployment with no version is unverified. learn-ai's pods get the account's endpoint as
`OL_LLM_AZURE_DECISION_ENDPOINT` once `learn_ai:enable_azure_decision` is set in that
environment, which has to wait for this stack to apply there.

Each identity holds one federated identity credential trusting the OIDC issuer of the
EKS cluster its consumer runs on (`applications` for the first three, `data` for
Dagster) for exactly the subject above, and one `Cognitive Services OpenAI
User` role assignment scoped to **its own** account. Account scope rather than resource
group scope is what stops one app's identity from reaching another app's endpoint, and
is the reason each consumer gets its own account rather than sharing one.

The account boundary also carries usage attribution, which has to come from the
resource: Azure Monitor's Cognitive Services metrics have no stable caller dimension.

## No admin consent, no directory writes

Everything here is an ARM resource under the subscription. A user-assigned managed
identity is not an Entra directory object, so this project needs no Microsoft Graph
permission, no directory role, and no tenant admin action — which is what makes it
deployable with the `Owner` grant we hold. The rejected design (Vault's Azure secrets
engine minting service principals) needed Graph `Application.ReadWrite.OwnedBy`, and
consenting to a Graph app role requires Privileged Role Administrator. See §9 of the
spec.

## Credentials

The Pulumi deploy service principal's credentials are read from
`src/bridge/secrets/pulumi/azure.{ci,qa,production}.yaml` (SOPS-encrypted), holding
`tenant_id`, `subscription_id`, `client_id`, and `client_secret`. That principal is
created manually, out of band, and needs `Contributor` + `User Access Administrator`
(or `Owner`) on the subscription. It needs **no** Microsoft Graph permission.

## Consuming the outputs

Application stacks take a `StackReference` on this project and read
`workload_identities[<consumer>]["client_id"]`, `tenant_id`, and
`cognitive_accounts[<consumer>]["endpoint"]`, plus `decision_accounts[<consumer>]["endpoint"]`
for a consumer with decision models. None of those are secret. Pods get a
projected ServiceAccount token with the `api://AzureADTokenExchange` audience plus the
four `AZURE_*` environment variables `azure-identity` reads; nothing is mounted from
Vault and nothing rotates.

## Verifying a deploy

```shell
az cognitiveservices account show -n "ol-openai-mitlearn-ci" -g "ol-openai-ci"
az identity federated-credential list \
  --identity-name "ol-openai-mitlearn-ci" -g "ol-openai-ci"
az role assignment list --scope "$(pulumi stack output cognitive_accounts | jq -r '.mitlearn.id')"
```

A federated credential with a wrong issuer or subject creates successfully and fails
only later, at token exchange, with `AADSTS70021: No matching federated identity record
found for presented assertion`. The same error appears for a few minutes after a
correct credential is created, because the record propagates asynchronously; Microsoft's
guidance is retry logic on every token request, not just the first.

## Model versions and capacity

Model versions are intentionally **not** pinned in code: which versions exist is a
property of the subscription and region, and a wrong version string fails at deployment
creation rather than at preview. Unset, Azure deploys its current default version and
upgrades it over time.

Once confirmed against the target subscription:

```shell
az cognitiveservices account list-models \
  -n "ol-openai-mitlearn-production" -g "ol-openai-production" \
  --query "[].{name:name, version:version}" -o table
```

set `azure_openai:model_versions` to a `{model: version}` map in the stack config.
Pinning also switches that deployment to `NoAutoUpgrade`, so a new default version
cannot change model behaviour under a running application with no deploy and no diff.

Production pins them to the versions CI and QA have been serving. CI and QA stay on
auto-upgrade, so they pick up a new default version first.

Capacity (`azure_openai:model_capacity`, in thousands of tokens per minute) defaults to
5 outside Production. **Production has no default and must be set explicitly** — the
stack raises at preview otherwise, because a default that asks for capacity nobody
confirmed turns a bootstrap into a partial deploy: the account and identity create, then
`Deployment` creation fails on quota. Production is set to 1000 (1M TPM per deployment),
except `gpt-5.4-pro` at 300, because its pool is too small for four deployments of 1000.

The `ol-engineering` subscription's `GlobalStandard` limits in capacity units (1 unit =
1K TPM):

| Model | Limit | Measured |
| --- | --- | --- |
| `gpt-4o` | 30000 | 2026-09-29 |
| `gpt-5-mini` | 10000 | 2026-09-29 |
| `gpt-5.2` | 30000 | 2026-09-29 |
| `gpt-5.4` | 10000 | 2026-11-16 |
| `gpt-5.4-mini` | 10000 | 2026-11-16 |
| `gpt-5.4-nano` | 150000 | 2026-11-16 |
| `gpt-5.4-pro` | 1600 | 2026-11-16 |
| `text-embedding-3-large` | 10000 | 2026-09-30 |
| `text-embedding-3-small` | 10000 | 2026-09-30 |

`azure_openai:model_capacities` overrides the capacity for a single model, for when one
pool needs a different size than `model_capacity`. `azure_openai:consumer_model_capacities`
(`{consumer: {model: capacity}}`) overrides it for one consumer's deployment of a model,
e.g. dagster-ml's QA `text-embedding-3-large` at 300, above the 250K-token requests its
embedder sends.

Check current usage against them with `az cognitiveservices usage list -l eastus`.

`Microsoft-Decision-1.1` draws on `AIServices.GlobalStandard.Microsoft-Decision-1.1`,
limit 10200 (2026-11-25), and Microsoft does not document its capacity unit. Production
sets it to 1000 through `model_capacities`, and it deploys for learn-ai only.

Quota is pooled **per model and version, per deployment type** — not one pool shared by
all models. These are `GlobalStandard` deployments, and [Microsoft's
docs](https://learn.microsoft.com/en-us/azure/ai-foundry/openai/quotas-limits) are
explicit that "deployments of the same model and version share one quota pool across
all regions in a subscription", so the pool is not per region either.

Concretely: the four `gpt-4o` deployments in an environment compete with the eight
`gpt-4o` deployments in the other two environments, and with nothing else. Capacity
planning sums twelve deployments per chat model (four consumers in three environments)
against that model's pool, three deployments (mitlearn's, one per environment) against
the `text-embedding-3-small` pool, and five (mitlearn's in each environment, dagster-ml's
in QA and Production) against the `text-embedding-3-large` pool. Raising a
non-production capacity still takes quota from Production, but only for that one model.

## Changing the deployed models

The chat models every consumer gets are `ModelConfig.models` in `__main__.py`. Models for
one consumer only (the embeddings) are `azure_openai:consumer_extra_models` in the QA and
Production stack config, with the code default covering CI. A stack can also set
`azure_openai:models`, but that replaces the whole default list for that stack, so prefer
editing the default.

To add a model:

1. Confirm the model can be deployed as `GlobalStandard` in the subscription and that it
   is not deprecating. Azure refuses new deployments of a model whose lifecycle is
   `Deprecating`, e.g. `gpt-4o-mini`.

   ```shell
   az cognitiveservices account list-models \
     -n "ol-openai-mitlearn-ci" -g "ol-openai-ci" \
     --query "[?name=='gpt-5.4'].{version:version, lifecycle:lifecycleStatus, skus:skus[].name}" \
     -o json
   ```

2. Check its quota pool. Each pool is subscription-wide and separate per model, so sum
   every deployment of the new model: Production's `model_capacity` (or its
   `model_capacities` override) times four consumers, plus 5 per deployment in CI and QA.
   That sum has to fit in `limit` minus `current`, since anything else in the
   subscription already deployed on the model draws from the same pool.

   ```shell
   az cognitiveservices usage list -l eastus \
     --query "[?name.value=='OpenAI.GlobalStandard.gpt-5.4'].{current:currentValue, limit:limit}" \
     -o table
   ```

   If that sum exceeds `limit` minus `current`, set `azure_openai:model_capacities` for the model in
   `Pulumi.Production.yaml` (e.g. `gpt-5.4-pro: 300`), or request more quota in the
   Azure portal first. Add the limit to the table above.

3. Add the model to `ModelConfig.models`.

4. Pin the version in `Pulumi.Production.yaml` under `azure_openai:model_versions`,
   using the version from step 1. CI and QA stay unpinned and auto-upgrade.

5. Update the model list at the top of this README, then `pulumi preview` each stack.
   Expect one new `Deployment` per consumer. `ModelConfig` fails the preview if a
   capacity or version names a model that is not deployed.

6. After CI applies, confirm the deployment is live and serving:

   ```shell
   az cognitiveservices account deployment list \
     -n "ol-openai-mitlearn-ci" -g "ol-openai-ci" \
     --query "[].{name:name, version:properties.model.version, state:properties.provisioningState}" \
     -o table
   ```

7. Add an `azure:<model>` entry with its price to the catalog in
   [`ol-llm`](https://github.com/mitodl/ol-llm) (`python/src/ol_llm/catalog.toml`), in a
   pull request the catalog owner approves (policy spec §2.2 item 3). The
   entry does not gate the deployment, but until it exists `build_model` warns on the
   model, runs through `ol-llm`'s entry points tag their traces
   `model_status:uncataloged`, and its tokens have no price in the spend rules.

To remove a model, first make sure no app still calls it, since requests to a deleted
deployment fail. Then delete it from the list, along with
its `model_versions` and capacity entries, which would otherwise fail validation.
Remove **one model per apply**. Pulumi deletes removed resources in parallel, and Azure
answers a second concurrent deployment operation on the same account with 409
`RequestConflict` (see the `deploy_models` docstring in `__main__.py`).

Version-only changes go through `azure_openai:model_versions` in Production and need no
code change.

## Local development

Developers authenticate as themselves. The `ol-eng-developer` Entra group (synced from
MIT's on-prem directory, set as `azure_openai:developer_group_object_id` in
`Pulumi.CI.yaml`) holds `Cognitive Services OpenAI User` on the four **CI** accounts and
`Cognitive Services User` on the CI decision account, and nothing on QA or Production.
The program refuses that key on any other stack. To
be added, ask the owners of the `owner-ol-eng-developer` list. After `az login`, `DefaultAzureCredential` resolves
`AzureCliCredential`, so the same application code that runs in the cluster (where it
resolves `WorkloadIdentityCredential`) works on a laptop with no environment branch.
