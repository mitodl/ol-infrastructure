# Project-Scoped Stacks Migration Guide

## Background

Pulumi's DIY (self-managed, S3) backend has deprecated the legacy flat-namespace
stack model and will remove support by end of 2025. This is tracked in
[pulumi/pulumi#19566](https://github.com/pulumi/pulumi/issues/19566).

Currently all 70 Pulumi projects share a single, global S3 namespace with dotted
stack names such as `applications.edxapp.mitx.QA`. This naming scheme was used to
avoid collisions and drove large amounts of parsing logic in `parse_stack()` and
across ~100 application files.

The migration moves to [project-scoped stacks](https://www.pulumi.com/blog/project-scoped-stacks-in-self-managed-backend/)
where each project has its own namespace. Stack names become short (e.g., `QA`,
`mitx.QA`, `applications.QA`).

## Migration Phases

| Phase | Description | Status |
|-------|-------------|--------|
| Phase 1 | Code preparation — backward-compatible helpers and shim | ✅ Merged (PR #4559) |
| Phase 2a | Add missing project constants to `pulumi_projects.py` | ✅ Merged (PR #4567) |
| Phase 3 | `pulumi state upgrade` — one-time, bucket-wide | ⬜ Pending |
| Phase 4 | Per-project stack renames (9 batches) + Pulumi.yaml updates | ⬜ Pending |
| Phase 5 | Remove backward-compat shim | ⬜ Pending |

---

## Architecture

### Stack Naming Convention (Post-Migration)

| Stack type | Example old name | Example new name |
|------------|-----------------|------------------|
| Single-env | `infrastructure.aws.network.QA` | `QA` |
| Multi-tenant | `applications.edxapp.mitx.QA` | `mitx.QA` |
| Cluster-scoped | `infrastructure.aws.eks.applications.QA` | `applications.QA` |
| Global (no env) | `infrastructure.aws.dns` | `default` |

### Project Naming Convention (Post-Migration)

| Tier | Pattern | Example |
|------|---------|---------|
| Applications | `ol-application-{service}` | `ol-application-mit-learn` |
| Infrastructure | `ol-infrastructure-{service}` | `ol-infrastructure-networking` |
| Substructure | `ol-substructure-{service}` | `ol-substructure-vault-auth` |

### Stack Reference Format (Post-Migration)

```python
# Before (legacy):
StackReference("applications.mit_learn.QA")

# After (project-scoped):
StackReference("organization/ol-application-mit-learn/QA")

# In code — use the stack_ref() helper which handles both formats:
from ol_infrastructure.lib.pulumi_helper import stack_ref
from ol_infrastructure.lib import pulumi_projects as projects

ref = stack_ref(projects.MIT_LEARN, "QA")
```

---

## Phase 1 — Code Preparation (Complete)

All backward-compatible helpers were added in PR #4559:

- **`StackInfo.project_name`** — new field holding the project's canonical name
- **`parse_stack()`** — dual-mode: handles both `applications.mit_learn.QA` (legacy)
  and `QA` / `mitx.QA` (new)
- **`stack_ref(project, stack)`** — returns the correct reference format based on
  whether the project is still in `LEGACY_PROJECT_PREFIXES`; while a project is in
  the shim dict, returns the old flat format; after removal, returns
  `organization/{project}/{stack}`
- **`LEGACY_PROJECT_PREFIXES`** in `pulumi_projects.py` — maps each project constant
  to its current legacy dotted stack prefix; entry is removed when the project is
  renamed
- All ~55 literal `StackReference()` calls replaced with `stack_ref()` calls

---

## Phase 2a — Add Missing Constants

Several projects were missing entries in `pulumi_projects.py`. These must be added
before the state upgrade so `stack_ref()` can correctly handle their stacks during
the transition period.

Projects needing new constants/`LEGACY_PROJECT_PREFIXES` entries:

| Directory | Current `Pulumi.yaml` name | Proposed constant | New project name |
|-----------|---------------------------|-------------------|-----------------|
| `applications/bootcamps` | `ol-infrastructure-bootcamps-ecommerce-application` | `BOOTCAMPS` | `ol-application-bootcamps` |
| `applications/clickhouse` | `ol-infrastructure-clickhouse-application` | `CLICKHOUSE` | `ol-application-clickhouse` |
| `applications/ecs_test` | `ol-infrastructure-ecs-test-application` | `ECS_TEST` | `ol-application-ecs-test` |
| `applications/mailgun` | `ol-infrastructure-mailgun-service` | `MAILGUN` | `ol-application-mailgun` |
| `applications/mit_learn_nextjs` | `ol-infrastructure-mit-learn-ne-application` | `MIT_LEARN_NEXTJS` | `ol-application-mit-learn-nextjs` |
| `applications/mitx` | `ol-infrastructure-mitx` | `MITX` | `ol-application-mitx` |
| `applications/ocw_site` | `ol-infrastructure-ocw-site-application` | `OCW_SITE` | `ol-application-ocw-site` |
| `applications/open_discussions` | `ol-infrastructure-open_discussions-application` | `OPEN_DISCUSSIONS` | `ol-application-open-discussions` |
| `applications/starrocks` | `ol-infrastructure-starrocks-application` | `STARROCKS_APP` | `ol-application-starrocks` |
| `applications/tika` | `ol-infrastructure-tika-server` | `TIKA` | `ol-application-tika` |
| `applications/xpro` | `ol-infrastructure-xpro-application` | `XPRO` | `ol-application-xpro` |
| `applications/xqueue` | `ol-infrastructure-xqueue-server` | `XQUEUE` | `ol-application-xqueue` |
| `applications/xqwatcher` | `ol-infrastructure-xqwatcher-server` | `XQWATCHER` | `ol-application-xqwatcher` |
| `infrastructure/gcp/gemini` | `ol-infrastructure-gemini_api` | `GEMINI_API` (exists) | `ol-infrastructure-gemini-api` (fix underscore) |

---

## Phase 3 — Global State Upgrade

> ⚠️ **This operation is irreversible and affects the entire S3 bucket.**

### Prerequisites

1. Phase 2a PR merged
2. `pulumi version` ≥ 3.61.0 on all Concourse workers and developer machines
3. Ensure `PULUMI_DIY_BACKEND_IGNORE_DEPRECATION_WARNING=1` is set in Concourse
   pipelines (suppresses the deprecation warning until all stack renames complete)

### What Happens

The S3 backend stores one `.pulumi/meta.yaml` at the bucket root.

- **Before upgrade:** `meta.yaml` is absent or has `version: 0` — all CLI commands
  use the legacy flat mode, stack names must be globally unique dotted strings
- **After upgrade:** `meta.yaml` is written with `version: 1` — all CLI commands
  use project-scoped mode, stacks are isolated per-project

The upgrade enumerates all 274 `.pulumi/stacks/*.json` flat files, reads the
project name from resource URNs within each state file
(`urn:pulumi:{stack}::{project}::...`), and moves each file to
`.pulumi/stacks/{project_name}/{stack_name}.json`.

### Command

Run from **any** project directory logged in to the S3 backend:

```bash
cd src/ol_infrastructure/infrastructure/aws/network
pulumi login s3://mitol-pulumi-state
pulumi state upgrade
# Type "yes" at the confirmation prompt
```

### Validate

```bash
# Still shows old dotted stack names at this point — that is expected
pulumi stack ls --all
```

---

## Phase 4 — Per-Project Stack Renames

The script [`scripts/pulumi/rename_stacks.sh`](../scripts/pulumi/rename_stacks.sh)
contains every `pulumi stack rename` command for all 274 stacks, organized by
project.

### Critical Sequencing

> **Do NOT** update `Pulumi.yaml` name AND run `pulumi up` before running the rename
> commands. Doing so would rewrite resource URNs in the state file, causing Pulumi to
> see all resources as new on the next `pulumi up`.

The correct order for each project is:

1. Run `pulumi stack rename` commands (state changes — script below)
2. Merge code PR: update `Pulumi.yaml` name, remove project from
   `LEGACY_PROJECT_PREFIXES`, rename `Pulumi.{old}.yaml` config files to
   `Pulumi.{new}.yaml`
3. Run `pulumi preview --stack {new-stack}` — must show **zero resource diff**
4. Run `pulumi preview` in any dependent project — verify stack references resolve

### How Rename Works

- For **Group A** (project name unchanged, 18 projects): use short form:
  ```bash
  pulumi stack rename --stack OLD_DOTTED_NAME NEW_SHORT_NAME
  ```
- For **Group B** (project name changes, 52 projects): use full org/project form:
  ```bash
  pulumi stack rename --stack OLD_DOTTED_NAME organization/NEW_PROJECT_NAME/NEW_STACK_NAME
  ```

`pulumi stack rename` atomically rewrites all resource URNs inside the state file
and moves the state file to the new location. Cloud resources are not touched.

### Recommended Batch Order

Rename leaf projects first, most-depended-upon infrastructure last, to minimize the
window where a `stack_ref()` might resolve to a non-existent stack name.

#### Batch 1 — Standalone Application Leaves

Projects with no other Pulumi projects depending on them:
`airbyte`, `b2b_partners_storage`, `bootcamps`, `celery_monitoring`, `clickhouse`,
`dagster`, `digital_credentials`, `ecs_test`, `fastly_redirector`, `jupyterhub`,
`keycloak` (application), `learn_ai`, `mailgun`, `micromasters`, `mit_learn`,
`mit_learn_nextjs`, `mitx`, `mitxonline`, `ocw_site`, `ocw_studio`,
`odl_video_service`, `open_discussions`, `redash`, `starburst`, `starrocks`
(application), `superset`, `tika`, `xpro`

#### Batch 2 — edX Cluster

`xqueue`, `xqwatcher`, `edx_notes`, `codejail`, `edxapp`, `concourse`

#### Batch 3 — Data / Analytics

`starrocks` (substructure), `open_metadata`

#### Batch 4 — EKS Substructure

`substructure/aws/eks`

#### Batch 5 — Vault Substructure

`vault/auth`, `vault/setup`, `vault/static_mounts`, `vault/encryption_mounts`,
`vault/pki`, `vault/secrets`

#### Batch 6 — Consul / Keycloak Substructure

`substructure/consul`, `substructure/keycloak`, `tls_certificates`,
`xpro_partner_dns`

#### Batch 7 — Infrastructure Leaf Projects

`data_warehouse`, `vector_log_proxy`, `grafana_cloud`, `qdrant_cloud`, `s3_sites`,
`sftp_servers`

#### Batch 8 — Core Infrastructure

`monitoring`, `mongodb_atlas`, `opensearch`, `vault` (server), `consul`
(infrastructure)

#### Batch 9 — Most-Depended-On Infrastructure (rename last)

`dns`, `ecr`, `iam`, `policies`, `private_ca`, `network`, `kms`, `eks`
(infrastructure)

---

## Phase 5 — Remove Backward-Compat Shim

After **all** projects are renamed, open a final cleanup PR:

### `src/ol_infrastructure/lib/pulumi_projects.py`

- Remove `LEGACY_PROJECT_PREFIXES` dict entirely
- Update `stack_ref()` to simply return `f"organization/{project_name}/{stack_name}"`

### `src/ol_infrastructure/lib/pulumi_helper.py`

- Remove the `_LEGACY_PREFIXES` detection tuple from `parse_stack()`
- `parse_stack()` should only handle short names (`QA`, `mitx.QA`, etc.) since
  Pulumi will always provide the short name after the upgrade
- Remove `namespace` field from `StackInfo` (it is always empty post-migration)
- Update `full_name` property to always use
  `f"organization/{self.project_name}/{self.name}"`

### Concourse Pipelines (`src/ol_concourse/`)

- Remove any stage-name logic that splits on `.` to detect environment
- Clean up any remaining hardcoded dotted stack names

### Validation

```bash
uv run ruff format src/
uv run ruff check src/
uv run mypy src/
uv run pytest tests/
```

---

## Project Inventory

### Group A: 18 Projects — Name Already Correct (96 stacks)

| Directory | Project name | Stack rename pattern |
|-----------|-------------|---------------------|
| `infrastructure/aws/dns` | `ol-infrastructure-aws-dns` | `infrastructure.aws.dns` → `default` |
| `infrastructure/aws/ecr` | `ol-infrastructure-ecr` | `infrastructure.aws.ecr` → `default` |
| `infrastructure/aws/eks` | `ol-infrastructure-eks` | `infrastructure.aws.eks.applications.QA` → `applications.QA` |
| `infrastructure/aws/iam` | `ol-infrastructure-aws-iam` | `infrastructure.aws.iam` → `default` |
| `infrastructure/aws/kms` | `ol-infrastructure-aws-kms` | `infrastructure.aws.kms.QA` → `QA` |
| `infrastructure/aws/network` | `ol-infrastructure-networking` | `infrastructure.aws.network.QA` → `QA` |
| `infrastructure/aws/opensearch` | `ol-infrastructure-opensearch` | `infrastructure.aws.opensearch.mitx.QA` → `mitx.QA` |
| `infrastructure/aws/policies` | `ol-infrastructure-aws-policies` | `infrastructure.aws.policies` → `default` |
| `infrastructure/aws/private_ca` | `ol-infrastructure-private-ca` | `infrastructure.aws.private_ca` → `default` |
| `infrastructure/aws/s3_sites` | `ol-infrastructure-aws-s3` | `infrastructure.aws.s3_sites.QA` → `QA` |
| `infrastructure/aws/sftp_servers` | `ol-infrastructure-aws-sftp` | `infrastructure.aws.sftp_servers.QA` → `QA` |
| `infrastructure/consul` | `ol-infrastructure-consul` | `infrastructure.consul.operations.QA` → `operations.QA` |
| `infrastructure/grafana_cloud` | `ol-infrastructure-grafana-cloud` | `infrastructure.grafana_cloud.Production` → `Production` |
| `infrastructure/mongodb_atlas` | `ol-infrastructure-mongodb-atlas` | `infrastructure.mongodb_atlas.mitx.QA` → `mitx.QA` |
| `infrastructure/monitoring` | `ol-infrastructure-monitoring` | `infrastructure.monitoring` → `default` |
| `infrastructure/qdrant_cloud` | `ol-infrastructure-qdrant-cloud` | `infrastructure.qdrant_cloud.mitlearn.QA` → `mitlearn.QA` |
| `infrastructure/vault` | `ol-infrastructure-vault-server` | `infrastructure.vault.operations.QA` → `operations.QA` |
| `substructure/aws/eks` | `ol-substructure-eks` | `substructure.aws.eks.applications.QA` → `applications.QA` |

### Group B: 52 Projects — Name Changes (178 stacks)

| Directory | Current name | New name |
|-----------|-------------|----------|
| `applications/airbyte` | `ol-infrastructure-airbyte-server` | `ol-application-airbyte` |
| `applications/b2b_partners_storage` | `ol-infrastructure-B2BPartnersStorage-application` | `ol-application-b2b-partners-storage` |
| `applications/bootcamps` | `ol-infrastructure-bootcamps-ecommerce-application` | `ol-application-bootcamps` |
| `applications/celery_monitoring` | `ol-infrastructure-celery-monitoring-application` | `ol-application-celery-monitoring` |
| `applications/clickhouse` | `ol-infrastructure-clickhouse-application` | `ol-application-clickhouse` |
| `applications/codejail` | `ol-infrastructure-codejail-server` | `ol-application-codejail` |
| `applications/concourse` | `ol-infrastructure-concourse-application` | `ol-application-concourse` |
| `applications/dagster` | `ol-infrastructure-dagster-application` | `ol-application-dagster` |
| `applications/digital_credentials` | `ol-infrastructure-digital-credentials-application` | `ol-application-digital-credentials` |
| `applications/ecs_test` | `ol-infrastructure-ecs-test-application` | `ol-application-ecs-test` |
| `applications/edx_notes` | `ol-infrastructure-edx-notes-application` | `ol-application-edx-notes` |
| `applications/edxapp` | `ol-infrastructure-edxapp-application` | `ol-application-edxapp` |
| `applications/fastly_redirector` | `ol-infrastructure-fastly-redirector` | `ol-application-fastly-redirector` |
| `applications/jupyterhub` | `ol-infrastructure-jupyterhub-application` | `ol-application-jupyterhub` |
| `applications/keycloak` | `ol-infrastructure-keycloak-application` | `ol-application-keycloak` |
| `applications/kubewatch` | `ol-infrastructure-kubewatch` | `ol-application-kubewatch` |
| `applications/kubewatch_webhook_handler` | `ol-infrastructure-kubewatch-webhook-handler` | `ol-application-kubewatch-webhook` |
| `applications/learn_ai` | `ol-infrastructure-learn_ai-application` | `ol-application-learn-ai` |
| `applications/mailgun` | `ol-infrastructure-mailgun-service` | `ol-application-mailgun` |
| `applications/micromasters` | `ol-infrastructure-micromasters-application` | `ol-application-micromasters` |
| `applications/mit_learn` | `ol-infrastructure-mitlearn-application` | `ol-application-mit-learn` |
| `applications/mit_learn_nextjs` | `ol-infrastructure-mit-learn-ne-application` | `ol-application-mit-learn-nextjs` |
| `applications/mitx` | `ol-infrastructure-mitx` | `ol-application-mitx` |
| `applications/mitxonline` | `ol-infrastructure-mitxonline-application` | `ol-application-mitxonline` |
| `applications/ocw_site` | `ol-infrastructure-ocw-site-application` | `ol-application-ocw-site` |
| `applications/ocw_studio` | `ol-infrastructure-ocw-studio-application` | `ol-application-ocw-studio` |
| `applications/odl_video_service` | `ol-infrastructure-odl-video-service-env` | `ol-application-odl-video-service` |
| `applications/open_discussions` | `ol-infrastructure-open_discussions-application` | `ol-application-open-discussions` |
| `applications/open_metadata` | `ol-infrastructure-open_metadata-application` | `ol-application-open-metadata` |
| `applications/redash` | `ol-infrastructure-redash-application` | `ol-application-redash` |
| `applications/starburst` | `ol-infrastructure-starburst-application` | `ol-application-starburst` |
| `applications/starrocks` | `ol-infrastructure-starrocks-application` | `ol-application-starrocks` |
| `applications/superset` | `ol-infrastructure-superset-application` | `ol-application-superset` |
| `applications/tika` | `ol-infrastructure-tika-server` | `ol-application-tika` |
| `applications/xpro` | `ol-infrastructure-xpro-application` | `ol-application-xpro` |
| `applications/xqueue` | `ol-infrastructure-xqueue-server` | `ol-application-xqueue` |
| `applications/xqwatcher` | `ol-infrastructure-xqwatcher-server` | `ol-application-xqwatcher` |
| `infrastructure/aws/data_warehouse` | `ol-infrastructure-data_warehouse` | `ol-infrastructure-data-warehouse` |
| `infrastructure/gcp/gemini` | `ol-infrastructure-gemini_api` | `ol-infrastructure-gemini-api` |
| `infrastructure/vector_log_proxy` | `ol-infrastructure-vector-log-proxy-server` | `ol-infrastructure-vector-log-proxy` |
| `substructure/consul` | `ol-infrastructure-substructure-consul` | `ol-substructure-consul` |
| `substructure/keycloak` | `ol-infrastructure-substructure-keycloak` | `ol-substructure-keycloak` |
| `substructure/starrocks` | `ol-infrastructure-substructure-starrocks` | `ol-substructure-starrocks` |
| `substructure/tls_certificates` | `ol-infrastructure-substructure-tls-certificates` | `ol-substructure-tls-certificates` |
| `substructure/vault/approle` | `ol-infrastructure-vault-approles` | `ol-substructure-vault-approles` |
| `substructure/vault/auth` | `ol-infrastructure-substructure-vault-auth` | `ol-substructure-vault-auth` |
| `substructure/vault/encryption_mounts` | `ol-infrastructure-substructure-vault-encryption-mounts` | `ol-substructure-vault-encryption-mounts` |
| `substructure/vault/pki` | `ol-infrastructure-vault-pki` | `ol-substructure-vault-pki` |
| `substructure/vault/secrets` | `ol-infrastructure-substructure-vault-secrets` | `ol-substructure-vault-secrets` |
| `substructure/vault/setup` | `ol-infrastructure-substructure-vault-setup` | `ol-substructure-vault-setup` |
| `substructure/vault/static_mounts` | `ol-infrastructure-substructure-vault-static-mounts` | `ol-substructure-vault-static-mounts` |
| `substructure/xpro_partner_dns` | `ol-infrastructure-substructure-xpro-partner-dns` | `ol-substructure-xpro-partner-dns` |

---

## Key Technical Notes

### Why state upgrade must precede stack renames

The `legacyReferenceStore.ParseReference` in the Pulumi CLI rejects stack names
containing `/` (via `tokens.ParseStackName` regex `^[A-Za-z0-9_.-]*$`). The slash
format `organization/project/stack` only becomes parseable by
`projectReferenceStore.ParseReference` after `pulumi state upgrade` writes
`meta.yaml version: 1` to the bucket.

### How `pulumi stack rename` works

`pulumi stack rename` calls `edit.RenameStack(chk.Latest, newStackName, newProjectName)`,
which atomically:
1. Rewrites all resource URNs in the state (project + stack components)
2. Moves the state file from `old-project/old-stack.json` to
   `new-project/new-stack.json`

Cloud resources are **not** touched. Only the Pulumi state and URN metadata change.

### The dangerous window

After running `pulumi stack rename` for a project, `LEGACY_PROJECT_PREFIXES` for
that project must be removed (or the project's entry updated) before any other
`stack_ref()` call attempts to resolve a reference to the now-renamed stack. To
minimize this window, the code PR (Pulumi.yaml + LEGACY_PROJECT_PREFIXES removal)
should be merged immediately after the rename commands complete.

### The `ecs_test` special case

The `applications/ecs_test` project has a single stack named `dev` (already a
plain, non-dotted name). No `LEGACY_PROJECT_PREFIXES` entry is needed for it
because `stack_ref(ECS_TEST, "dev")` with no prefix in the dict will directly
return `organization/ol-application-ecs-test/dev`.

### Zero-stack projects

`infrastructure/gcp/gemini`, `substructure/tls_certificates`, and
`substructure/vault/approle` currently have no active stacks. For these projects
Phase 4 is code-only: update `Pulumi.yaml` name, no `pulumi stack rename` commands
needed.
