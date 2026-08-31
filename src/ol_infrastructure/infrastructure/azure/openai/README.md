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

Each account gets a deployment of `gpt-4o`, `gpt-4o-mini`, and `gpt-5.2`.

Each identity holds one federated identity credential trusting the environment's EKS
cluster OIDC issuer for exactly the subject above, and one `Cognitive Services OpenAI
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
`cognitive_accounts[<consumer>]["endpoint"]`. None of those are secret. Pods get a
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

Capacity (`azure_openai:model_capacity`, in thousands of tokens per minute) defaults to
50 for Production and 5 elsewhere. TPM quota is allocated **per subscription, per
region, per model** and is shared across every account, so all 27 deployments across the
three environments divide one pool. Raising a non-production capacity takes quota away
from Production.

## Local development

Developers authenticate as themselves. One Entra security group holding the engineers
who need it is granted `Cognitive Services OpenAI User` on the three **CI** accounts
only — nothing on QA or Production. After `az login`, `DefaultAzureCredential` resolves
`AzureCliCredential`, so the same application code that runs in the cluster (where it
resolves `WorkloadIdentityCredential`) works on a laptop with no environment branch.
