# Rootly Pulumi known issues and follow-ups

This stack manages Rootly SaaS resources via the generated Pulumi SDK for the
Rootly Terraform provider (`rootlyhq/rootly` v5.16.1). The initial migration
imported the operationally important resources that could be represented with a
clean no-op Pulumi preview.

Last verified preview state for this branch:

```text
PULUMI_TERRAFORM_VERSION=1.5.0 pulumi preview --diff
Resources:
    149 unchanged
```

## Required local workaround

Until upstream fixes land, Pulumi operations for this stack require a patched
`pulumi-resource-terraform-provider` bridge and this environment variable:

```bash
export PULUMI_TERRAFORM_VERSION=1.5.0
```

Root cause: Pulumi's dynamic Terraform bridge sends
`TerraformVersion: "pulumi-terraform-bridge"` during provider configuration.
The Rootly provider parses that field as semantic version text with
`goversion.Must(...)`, so the provider panics and Pulumi reports only:

```text
error calling ConfigureProvider: rpc error: code = Unavailable desc = error reading from server: EOF
```

Tracking issues:

- Rootly provider: <https://github.com/rootlyhq/terraform-provider-rootly/issues/379>
- Pulumi bridge: <https://github.com/pulumi/pulumi-terraform-bridge/issues/3514>
- Internal follow-up (blocked by the two issues above): <https://github.com/mitodl/ol-infrastructure/issues/4906>

## Concourse pipeline is currently non-functional

The `pulumi-rootly` Concourse pipeline (added in
[#4905](https://github.com/mitodl/ol-infrastructure/pull/4905)) exists and is wired
up correctly, but the `deploy-ol-saas-rootly-production` job cannot currently
complete a `pulumi up`. Triggering it reproduces the exact same upstream bug
described above:

```text
pulumi:pulumi:Stack ol-saas-rootly-Production  warning: refresh operation is using an older version of package 'rootly' than the specified program version: 1.1.4 < 5.16.1
...
error: error calling ConfigureProvider: rpc error: code = Unavailable desc = error reading from server: EOF
(repeated for every rootly:index resource in the stack)

Resources:
    2 unchanged
    148 errored

Duration: 1s
```

The shared `pulumi-provisioner` Concourse resource type (image
`mitodl/concourse-pulumi-resource-provisioner`, source repo
`mitodl/concourse-pulumi-resource`, currently **archived**) downloads the stock,
unpatched Terraform bridge plugin at runtime and does not set
`PULUMI_TERRAFORM_VERSION`. The local workaround above only exists on individual
developer machines, not in the CI environment.

**Decision:** we are intentionally not patching the shared
`mitodl/concourse-pulumi-resource-provisioner` image, since it is used by every
Pulumi pipeline in the fleet and its source repo is archived. Instead, this stack
will remain manual-only (`pulumi up` from a developer machine with the patched
bridge and `PULUMI_TERRAFORM_VERSION=1.5.0`) until the upstream issues are fixed.
Follow-up tracked in
[mitodl/ol-infrastructure#4906](https://github.com/mitodl/ol-infrastructure/issues/4906),
which is blocked by the two upstream issues linked above.

## Imported resources

The old `ol-rootly-manager` import inventory contained 317 resources. This
migration currently imports 147 of those inventory resources into Pulumi. The
current Pulumi preview has 149 unchanged resources because it also includes the
Pulumi stack and explicit Rootly provider resource.

Imported categories include:

- Severities, roles, environments, incident causes, incident types, and incident role
- Platform Engineering team
- Rootly services, excluding the empty-state `christest` service
- On-call schedule, schedule rotation, rotation users, escalation policies, and escalation levels
- Dashboards and dashboard panels
- Incident permission sets and resource-scoped incident permissions
- Alert sources and alert routes, excluding the empty-state Chris Test route

## Remaining inventory

The remaining 170 inventory resources are:

- 61 `rootly:index/formFieldPosition:FormFieldPosition`
- 48 `rootly:index/incidentPermissionSetBoolean:IncidentPermissionSetBoolean`
- 28 `rootly:index/formField:FormField`
- 5 `rootly:index/workflowIncident:WorkflowIncident`
- 5 `rootly:index/retrospectiveStep:RetrospectiveStep`
- 4 `rootly:index/workflowGroup:WorkflowGroup`
- 3 `rootly:index/functionality:Functionality`
- 3 `rootly:index/playbook:Playbook`
- 2 `rootly:index/statusPage:StatusPage`
- 2 `rootly:index/workflowActionItem:WorkflowActionItem`
- 2 `rootly:index/retrospectiveConfiguration:RetrospectiveConfiguration`
- 1 `rootly:index/alertRoute:AlertRoute`
- 1 `rootly:index/service:Service`
- 1 `rootly:index/team:Team`
- 1 `rootly:index/escalationPolicy:EscalationPolicy`
- 1 `rootly:index/retrospectiveProcess:RetrospectiveProcess`
- 1 `rootly:index/postMortemTemplate:PostMortemTemplate`
- 1 `rootly:index/escalationLevel:EscalationLevel`

## Deferred/provider-problematic resources

Some resources were deliberately removed from Pulumi state after import because
Rootly returned no readable state or the provider schema could not model the
resource without diffs or validation errors.

### Empty state after import

The provider returned empty state for these imports, which would make Pulumi plan
protected replacements if they were modeled in code:

- `rootly:index/team:Team::christestteam`
- `rootly:index/service:Service::christest`
- `rootly:index/functionality:Functionality::{search,checkout,login}`
- `rootly:index/escalationPolicy:EscalationPolicy::christest-escalation-policy`
- `rootly:index/escalationLevel:EscalationLevel::r-7f50a094-1f88-4e75-9afd-5e56ea8448a9`
- `rootly:index/alertRoute:AlertRoute::chris-test-route`

These are likely stale/example objects or provider read-path bugs. Re-try only
with preview-only imports first.

### `IncidentPermissionSetBoolean`

The 48 `IncidentPermissionSetBoolean` resources import successfully, but the
provider stores `incidentPermissionSetBooleanId` in state as a read-only field.
If the field is omitted, Pulumi plans updates. If the field is included, provider
validation fails with `Invalid or unknown key`. `ignore_changes` does not help
because the invalid field still reaches provider validation.

These resources are deferred until the provider schema/read behavior is fixed or
we choose to leave them unmanaged.

### Alert source normalization

Alert source webhook secrets are stored in `src/bridge/secrets/rootly/account.yaml`
under `alert_source_secrets` and passed with `Output.secret(...)`.

`rootly_alert_source_opts` ignores changes to:

- `secret` — Pulumi/provider secret comparison churn caused no-op diffs even when
  the SOPS value matched the imported state value.
- `resolutionRuleAttributes` — Pingdom's resolution rule was provider-normalized
  into a diff during no-op previews.

The SOPS secret file is currently KMS-encrypted. Add Vault transit recipients with
`sops updatekeys` when suitable Vault access is available.

## Validation commands

From `src/ol_infrastructure/saas/rootly`:

```bash
PULUMI_TERRAFORM_VERSION=1.5.0 pulumi preview --diff
```

From the repository root:

```bash
uv run ruff check src/ol_infrastructure/saas/rootly/__main__.py
uv run mypy src/ol_infrastructure/saas/
```

Repo-wide `pre-commit run --all-files` was attempted during the migration, but
this environment lacked `packer`, and unrelated Dockerfile hadolint warnings
already exist in the repository.
