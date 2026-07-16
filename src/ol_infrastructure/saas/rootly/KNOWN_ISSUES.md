# Rootly Pulumi known issues and follow-ups

This stack manages Rootly SaaS resources via the generated Pulumi SDK for the
Rootly Terraform provider (`rootlyhq/rootly` v5.17.2, vendored at
`sdks/rootly`). The initial migration imported the operationally important
resources that could be represented with a clean no-op Pulumi preview.

Last verified preview state for this branch (stock bridge, no workarounds):

```text
pulumi preview --diff
Resources:
    ~ 1 to update   # provider bridge version 1.1.4 -> 1.2.0 (expected, one-time)
    144 unchanged
```

## Upstream bug and required workaround (now fixed in CI and locally)

Pulumi operations for this stack require a patched `pulumi-resource-terraform-provider`
bridge and this environment variable:

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

Upstream tracking issues (still open as of this writing — neither project has
released a fix):

- Rootly provider: <https://github.com/rootlyhq/terraform-provider-rootly/issues/379>
- Pulumi bridge: <https://github.com/pulumi/pulumi-terraform-bridge/issues/3514>
- Internal follow-up: <https://github.com/mitodl/ol-infrastructure/issues/4906> (closed —
  superseded by the CI-side fix below; reopen if the workaround needs revisiting)

**The team decided to patch our own tooling instead of waiting on upstream.** Two
places now carry the fix:

1. **Local developer machines:** manually patch the `pulumi-resource-terraform-provider`
   bridge binary (built from a pinned `pulumi-terraform-bridge` commit with a small
   patch that reads `PULUMI_TERRAFORM_VERSION` instead of hardcoding the bad string),
   install it at `~/.pulumi/plugins/resource-terraform-provider-v1.1.4/`, and set
   `PULUMI_TERRAFORM_VERSION=1.5.0`.
2. **Concourse CI:** [mitodl/ol-concourse#53](https://github.com/mitodl/ol-concourse/pull/53)
   (merged) adds the same patch to `resources/pulumi/Dockerfile.provisioner` — the
   actual, current source for the `mitodl/concourse-pulumi-resource-provisioner`
   image used by every `pulumi-provisioner` Concourse resource in the fleet. (The
   `mitodl/concourse-pulumi-resource` repo referenced in earlier investigation is
   archived and only points to `mitodl/ol-concourse` now.) A Renovate custom manager
   tracks the pinned bridge commit so a human is nudged to revisit this once upstream
   ships a fix — see that PR for details on why `automerge` is disabled for it.

## Concourse pipeline is now working

The `pulumi-rootly` Concourse pipeline (added in
[#4905](https://github.com/mitodl/ol-infrastructure/pull/4905)) runs successfully.
Confirmed via a real production run of `deploy-ol-saas-rootly-production` against
the patched provisioner image (`mitodl/concourse-pulumi-resource-provisioner:latest`,
rebuilt from ol-concourse#53):

```text
Resources:
    ~ 1 updated
    148 unchanged
```

## Upstream bug and required workaround (now fixed in CI and locally)

Pulumi operations for this stack require a patched `pulumi-resource-terraform-provider`
bridge and this environment variable:

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

Upstream tracking issues (still open as of this writing — neither project has
released a fix):

- Rootly provider: <https://github.com/rootlyhq/terraform-provider-rootly/issues/379>
- Pulumi bridge: <https://github.com/pulumi/pulumi-terraform-bridge/issues/3514>
- Internal follow-up: <https://github.com/mitodl/ol-infrastructure/issues/4906> (closed —
  superseded by the CI-side fix below; reopen if the workaround needs revisiting)

**The team decided to patch our own tooling instead of waiting on upstream.** Two
places now carry the fix:

1. **Local developer machines:** manually patch the `pulumi-resource-terraform-provider`
   bridge binary (built from a pinned `pulumi-terraform-bridge` commit with a small
   patch that reads `PULUMI_TERRAFORM_VERSION` instead of hardcoding the bad string),
   install it at `~/.pulumi/plugins/resource-terraform-provider-v1.1.4/`, and set
   `PULUMI_TERRAFORM_VERSION=1.5.0`.
2. **Concourse CI:** [mitodl/ol-concourse#53](https://github.com/mitodl/ol-concourse/pull/53)
   (merged) adds the same patch to `resources/pulumi/Dockerfile.provisioner` — the
   actual, current source for the `mitodl/concourse-pulumi-resource-provisioner`
   image used by every `pulumi-provisioner` Concourse resource in the fleet. (The
   `mitodl/concourse-pulumi-resource` repo referenced in earlier investigation is
   archived and only points to `mitodl/ol-concourse` now.) A Renovate custom manager
   tracks the pinned bridge commit so a human is nudged to revisit this once upstream
   ships a fix — see that PR for details on why `automerge` is disabled for it.

## Concourse pipeline is now working

The `pulumi-rootly` Concourse pipeline (added in
[#4905](https://github.com/mitodl/ol-infrastructure/pull/4905)) runs successfully.
Confirmed via a real production run of `deploy-ol-saas-rootly-production` against
the patched provisioner image (`mitodl/concourse-pulumi-resource-provisioner:latest`,
rebuilt from ol-concourse#53):

```text
Resources:
    ~ 1 updated
    148 unchanged
```

## RESOLVED: upstream panic bug and the patched-bridge workaround

Provider v5.17.1 fixed the panic on non-semver `TerraformVersion`
([rootlyhq/terraform-provider-rootly#385](https://github.com/rootlyhq/terraform-provider-rootly/pull/385),
closing [#379](https://github.com/rootlyhq/terraform-provider-rootly/issues/379)).
This stack now pins provider v5.17.2 and the stock
`pulumi-resource-terraform-provider` bridge v1.2.0, verified with a clean
`pulumi preview` and **no** `PULUMI_TERRAFORM_VERSION` environment variable and
**no** patched bridge binary.

Cleanup that can now happen (not blocking — the workarounds are inert):

- **Local developer machines:** the patched bridge binary at
  `~/.pulumi/plugins/resource-terraform-provider-v1.1.4/` is no longer used
  (the SDK pins v1.2.0, which Pulumi downloads from the official release);
  it can be deleted. `PULUMI_TERRAFORM_VERSION` no longer needs to be set.
- **Concourse CI:** the patch in `resources/pulumi/Dockerfile.provisioner`
  ([mitodl/ol-concourse#53](https://github.com/mitodl/ol-concourse/pull/53))
  is now unused for the same reason and can be reverted, along with its
  Renovate custom manager.
- The Pulumi bridge side of the bug
  ([pulumi/pulumi-terraform-bridge#3514](https://github.com/pulumi/pulumi-terraform-bridge/issues/3514))
  is still open upstream, with a fix in flight
  ([#3536](https://github.com/pulumi/pulumi-terraform-bridge/pull/3536)), but it
  no longer affects this stack because the provider tolerates the bad value.

## Provider 5.16.1 -> 5.17.2 upgrade notes

- `slug` became **output-only** on all resource types (Role, Severity,
  Environment, Cause, IncidentType, IncidentRole, Service, Team,
  IncidentPermissionSet, ...). All `slug=` inputs were removed from
  `__main__.py`; slugs are read from the API and produce no diffs.
- The provider now emits a deprecation warning for `ScheduleRotationUser`:
  it will be **removed in the next provider major version** in favor of the
  `schedule_rotation_members` attribute on `rootly_schedule_rotation`. This
  has since been migrated — see "Legacy `ScheduleRotationUser` resources
  removed" below.

## Imported resources

The old `ol-rootly-manager` import inventory contained 317 resources. This
migration currently imports 143 of those inventory resources into Pulumi (147
originally, less the 4 legacy `ScheduleRotationUser` resources removed — see
below). The current Pulumi preview has 144 unchanged resources because it also
includes the Pulumi stack and explicit Rootly provider resource.

Imported categories include:

- Severities, roles, environments, incident causes, incident types, and incident role
- Platform Engineering team
- Rootly services, excluding the empty-state `christest` service
- On-call schedule, schedule rotation, and rotation members (via `schedule_rotation_members`),
  escalation policies, and escalation levels
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

These are likely stale/example objects. The cleanest fix is deleting them in
the Rootly UI (they are test artifacts, not operational resources); otherwise
re-try only with preview-only imports first.

### `IncidentPermissionSetBoolean`

The 48 `IncidentPermissionSetBoolean` resources import successfully, but the
provider stores `incidentPermissionSetBooleanId` in state as a read-only field.
If the field is omitted, Pulumi plans updates. If the field is included, provider
validation fails with `Invalid or unknown key`. `ignore_changes` does not help
because the invalid field still reaches provider validation.

Re-checked against provider v5.17.2: the generated schema for this resource is
unchanged from v5.16.1 (the codegen fixes in
[rootlyhq/terraform-provider-rootly#382](https://github.com/rootlyhq/terraform-provider-rootly/pull/382)
did not touch it), so these remain deferred until the provider schema/read
behavior is fixed or we choose to leave them unmanaged.

Filed upstream:
[rootlyhq/terraform-provider-rootly#389](https://github.com/rootlyhq/terraform-provider-rootly/issues/389).
Re-test with a preview-only import once that ships in a release.

### Alert source normalization

Alert source webhook secrets are stored in `src/bridge/secrets/rootly/account.yaml`
under `alert_source_secrets` and passed with `Output.secret(...)`.

All alert sources ignore changes to `secret`: Pulumi/provider secret comparison
churn plans no-op updates even when the SOPS value matches the stored state
value (still true with provider v5.17.2 / bridge v1.2.0).

Only the Pingdom alert source additionally ignores `resolutionRuleAttributes`:
its resolution rule is provider-normalized into a no-op diff during previews
(re-verified against v5.17.2). The other alert sources no longer ignore this
field, so real resolution-rule changes to them are visible in previews.

The SOPS secret file is currently KMS-encrypted. Add Vault transit recipients with
`sops updatekeys` when suitable Vault access is available.

### Legacy `ScheduleRotationUser` resources removed

The provider deprecates `rootly_schedule_rotation_user` in favor of the
`schedule_rotation_members` attribute on `rootly_schedule_rotation` (removal
planned for the next provider major version). The primary rotation's
`ScheduleRotation` resource already declared `schedule_rotation_members` with
the same 4 people/positions as 4 separately-managed `ScheduleRotationUser`
resources — Rootly was carrying two parallel membership record sets for the
same rotation (legacy `schedule_rotation_users`, created 2025-05-09, alongside
newer `schedule_rotation_members`, created 2025-10-09).

Verified via the live Shifts API (`GET /v1/schedules/{id}/shifts`) that actual
on-call computation already used the `schedule_rotation_members` set (one
person per week, no duplication), so the legacy records were inert. Removed
the 4 `ScheduleRotationUser` resources from Pulumi state with
`pulumi state delete --force` (state-only; no Rootly API calls, so the live
schedule was unaffected) and deleted the corresponding code. The 4 legacy
`schedule_rotation_users` API records themselves still exist in Rootly,
unmanaged and orphaned — deleting them via the Rootly UI/API is optional
future cleanup, not urgent since they're inert.

## Validation commands

From `src/ol_infrastructure/saas/rootly`:

```bash
pulumi preview --diff
```

From the repository root:

```bash
uv run ruff check src/ol_infrastructure/saas/rootly/__main__.py
uv run mypy src/ol_infrastructure/saas/
```
