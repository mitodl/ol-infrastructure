# Azure OpenAI infrastructure

Provisions the Azure side of the Vault-managed Azure OpenAI credentials work. The Vault
mount that consumes this project's outputs lives in
`src/ol_infrastructure/substructure/vault/azure`; the full design is in
`docs/plans/vault-azure-openai-credentials-spec.md`.

## What this creates, per environment

One resource group (`ol-openai-{env}`) holding one Cognitive Services account per app
consumer:

| Consumer | Account |
| --- | --- |
| mit-learn | `ol-openai-mitlearn-{env}` |
| learn-ai | `ol-openai-learn-ai-{env}` |
| edxapp / mitxonline | `ol-openai-mitxonline-{env}` |

Each account gets a deployment of `gpt-4o`, `gpt-4o-mini`, and `gpt-5.2`. Alongside them,
one Azure AD application + service principal + password that Vault uses as the **root
credential** for its Azure secrets engine, granted `User Access Administrator` scoped to
the resource group.

The account boundary follows the consumer because usage attribution has to come from the
resource: Azure Monitor's Cognitive Services metrics have no stable caller dimension, and
Vault mints a fresh service principal on every lease, so grouping usage by calling
identity does not work.

## Required manual step: admin consent

**Pulumi creates the Graph permission request but cannot grant consent.** Every resource
in this project applies cleanly without it, and the failure surfaces much later — as an
Azure AD error when Vault first tries to mint credentials
(`vault read azure-openai/creds/ol-mitlearn-openai`).

After the first successful deploy of each environment's stack, a tenant Global Admin must
run, once per environment:

```shell
az ad app permission admin-consent --id "$(pulumi stack output vault_root_sp_client_id)"
```

Verify it took:

```shell
az ad app permission list-grants --id "$(pulumi stack output vault_root_sp_client_id)"
```

## Credentials

The Pulumi deploy service principal's credentials are read from
`src/bridge/secrets/pulumi/azure.{ci,qa,production}.yaml` (SOPS-encrypted), holding
`tenant_id`, `subscription_id`, `client_id`, and `client_secret`. That principal is
created manually, out of band — it needs `Contributor` + `User Access Administrator` (or
`Owner`) on the subscription plus Graph `Application.ReadWrite.All`, which is more
authority than a project should grant itself.

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

set `azure_openai:model_versions` to a `{model: version}` map in the stack config. Pinning
also switches that deployment to `NoAutoUpgrade`, so a new default version cannot change
model behaviour under a running application with no deploy and no diff.

Capacity (`azure_openai:model_capacity`, in thousands of tokens per minute) defaults to 50
for Production and 5 elsewhere. TPM quota is allocated **per subscription, per region, per
model** and is shared across every account, so all 27 deployments across the three
environments divide one pool. Raising a non-production capacity takes quota away from
Production.
