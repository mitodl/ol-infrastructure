# Separating Stateful Services into Dedicated Data Stacks

## Problem

Today, every application stack (e.g., `applications.mit_learn.production`) owns its
own RDS instance, ElastiCache cluster, and related security groups in the same Pulumi
project as the K8s workloads, Vault auth, APISIX routes, and DNS records. This means
a slow RDS operation—blue/green upgrades, storage scaling, minor version patches—
blocks an unrelated code change from deploying.

## Proposed Solution

Extract all stateful resources (RDS, ElastiCache, their security groups, and the Vault
DB backend registration) into a new **data sub-stack** per application. The application
stack becomes a pure deployment stack that consumes the data stack's outputs via
`StackReference`.

```
Before:
  applications.mit_learn.production
    ├── ec2.SecurityGroup  (app SG)
    ├── ec2.SecurityGroup  (db SG)
    ├── ec2.SecurityGroup  (cache SG)
    ├── OLAmazonDB         (RDS)
    ├── OLAmazonCache      (ElastiCache)
    ├── OLVaultDatabaseBackend
    ├── K8s HelmReleases / Deployments
    ├── APISIX routes
    ├── Fastly service
    └── Route53 records

After:
  applications.mit_learn.data.production   ← slow release cycle
    ├── ec2.SecurityGroup  (app SG)  ← moved here to avoid circular deps
    ├── ec2.SecurityGroup  (db SG)
    ├── ec2.SecurityGroup  (cache SG)
    ├── OLAmazonDB
    ├── OLAmazonCache
    └── OLVaultDatabaseBackend

  applications.mit_learn.production        ← fast release cycle
    ├── StackReference → data stack
    ├── K8s HelmReleases / Deployments
    ├── APISIX routes
    ├── Fastly service
    └── Route53 records
```

## Key Architectural Decisions

### 1. Security Group Ownership

The application security group (pod SG) is referenced by both the DB SG ingress rules
AND the K8s workloads. Keeping it in the app stack creates a **circular dependency**:

- App stack needs DB address → references data stack
- Data stack needs app SG ID → references app stack → CIRCULAR

**Decision**: Move the app SG, db SG, and cache SG into the **data stack**. The app
stack references the data stack for `app_security_group_id`. This eliminates the
circular dependency cleanly.

### 2. Vault DB Backend Ownership

The `OLVaultDatabaseBackend` is tightly coupled to the DB lifecycle (it needs the DB
address, admin credentials, and the Vault provider). Moving it to the data stack means
DB-level credentials are managed independently of the application release cycle.

DB role names (e.g., `mitlearn-app`) are deterministic strings, so the app stack can
reference them without a runtime dependency.

**Decision**: Move `OLVaultDatabaseBackend` to the data stack.

### 3. Passwords in Config

DB passwords are currently in `Pulumi.<stack>.yaml` for the application stacks.
The data stacks will have their own `Pulumi.<stack>.yaml` config files that include the
password (encrypted with SOPS per the existing KMS pattern).

## Directory Structure

Each data stack is a new Pulumi project under a `data/` subdirectory:

```
src/ol_infrastructure/applications/<app>/
  __main__.py                              ← updated (removes DB/cache/SGs)
  data/
    __main__.py                            ← NEW
    Pulumi.yaml                            ← NEW
    Pulumi.applications.<app>.data.CI.yaml
    Pulumi.applications.<app>.data.QA.yaml
    Pulumi.applications.<app>.data.Production.yaml
  k8s_secrets.py                           ← updated to accept plain values
```

## Stack Naming Convention

Following the existing pattern:

- Data stack project name: `ol-infrastructure-<app>-data`
- Stack names: `applications.<app>.data.CI`, `applications.<app>.data.QA`,
  `applications.<app>.data.Production`
- Reference in code: `StackReference(f"applications.{app_slug}.data.{stack_info.name}")`

## Standard Data Stack Exports

Each data stack exports a standardized dictionary:

```python
pulumi.export("<app_slug>_data", {
    # Security groups
    "app_security_group_id": app_sg.id,
    "app_security_group_name": app_sg.name,
    "db_security_group_id": db_sg.id,
    # Database
    "db_address": db.db_instance.address,
    "db_port": db.db_instance.port,
    "db_identifier": db.db_instance.identifier,
    # Cache (if applicable)
    "cache_address": cache.address,
    "cache_auth_token": cache.cache_cluster.auth_token,
})
```

## Updated Application Stack Pattern

```python
# In the application __main__.py
data_stack = StackReference(f"applications.mit_learn.data.{stack_info.name}")
data = data_stack.require_output("mitlearn_data")

# Replace direct resource attribute access:
# Before: mitlearn_db.db_instance.address
# After:  data["db_address"]

# Before: redis_cache.address
# After:  data["cache_address"]

# Before: mitlearn_app_security_group.id
# After:  data["app_security_group_id"]
```

## Migration Procedure (per application)

The migration uses `pulumi state move` — **no resources are recreated**.

### Step 1: Create data stack code

Write `data/__main__.py` with the extracted resources. Create `Pulumi.yaml` and
per-environment `Pulumi.<stack>.yaml` config files (copying password secrets from the
app stack config files).

### Step 2: Initialize the data stack in Pulumi

```bash
cd src/ol_infrastructure/applications/<app>/data
pulumi stack init applications.<app>.data.Production
# Copy encrypted password config values from old stack config
```

### Step 3: State move (zero-downtime)

From the original application stack directory, retrieve URNs then move each component:

```bash
# Show URNs of all resources in the app stack
pulumi stack --show-urns

# Move RDS component (all children — parameter group, CW alarms, IAM role — follow automatically)
pulumi state move \
  --source <org>/ol-infrastructure-<app>-application/applications.<app>.Production \
  --dest <org>/ol-infrastructure-<app>-data/applications.<app>.data.Production \
  'urn:pulumi:...::ol:infrastructure:aws:database:OLAmazonDB::<instance-name>'

# Move ElastiCache component (if applicable)
pulumi state move \
  --source ... --dest ... \
  'urn:pulumi:...::ol:infrastructure:aws:elasticache:OLAmazonCache::<cluster-name>'

# Move security groups (app SG, db SG, cache SG)
pulumi state move \
  --source ... --dest ... \
  'urn:pulumi:...::aws:ec2/securityGroup:SecurityGroup::<name>'

# Move Vault DB backend
pulumi state move \
  --source ... --dest ... \
  'urn:pulumi:...::OLVaultDatabaseBackend::<name>'
```

### Step 4: Update code and verify

1. Align `data/__main__.py` resource definitions (names/options) to match the moved URNs
2. Update app `__main__.py` to use `StackReference` for all data stack outputs
3. Run `pulumi preview` on the **data stack** → expect zero planned changes
4. Run `pulumi preview` on the **app stack** → expect zero planned changes

## Applications Inventory

### Has DB + Cache (both to move)

| Application | DB Engine | Vault Backend | K8s Integration |
|---|---|---|---|
| mit_learn | Postgres | Yes | Yes (`k8s_secrets.py`) |
| mitxonline | Postgres | Yes | Yes (`k8s_secrets.py`) |
| ocw_studio | Postgres | Yes | Yes (`k8s_secrets.py`) |
| odl_video_service | Postgres | Yes | Yes (`k8s_secrets.py`) |
| xpro | Postgres | Yes | Yes (`k8s_secrets.py`) |
| micromasters | Postgres | Yes | No (EC2-based) |
| redash | Postgres | Yes | No (EC2-based) |
| superset | Postgres | Yes | No (EC2-based) |
| edxapp | MariaDB | Yes | Yes (k8s_resources, k8s_secrets, k8s_configmaps, k8s_autoscaling) |
| learn_ai | — (cache only) | — | No |

### DB Only (no cache)

| Application | DB Engine | Vault Backend | K8s Integration |
|---|---|---|---|
| concourse | Postgres | Yes | No |
| dagster | Postgres | Yes | No |
| keycloak | Postgres | Yes | No |
| jupyterhub | Postgres | Yes | Yes (`deployment.py`) |
| open_metadata | Postgres | Yes | No |
| airbyte | Postgres | Yes | No |
| bootcamps | Postgres | Yes | No |

## Implementation Phases

### Phase 1 — Pilot: `mit_learn` + `mitxonline`

Both applications have DB + Cache + K8s secrets integration, making them thorough test
cases. Complete one full end-to-end cycle before proceeding to other applications.

- Create `data/__main__.py` for both apps
- Create `Pulumi.yaml` and per-env stack config files for both
- Execute state moves for all 3 environments (CI, QA, Production)
- Update application `__main__.py` and `k8s_secrets.py`
- Verify zero-diff `pulumi preview` on all 4 stacks per env

### Phase 2 — Batch A: Simple apps (DB-only, no K8s)

Lower risk due to simpler resource topology.

Applications: `concourse`, `airbyte`, `dagster`, `keycloak`, `bootcamps`

### Phase 3 — Batch B: DB + Cache with K8s

Applications: `ocw_studio`, `odl_video_service`, `xpro`, `micromasters`, `redash`,
`superset`

### Phase 4 — Complex apps

- `jupyterhub` — `deployment.py` integration pattern differs from standard `k8s_secrets.py`
- `open_metadata` — straightforward but lower priority
- `learn_ai` — cache-only (no DB); simplest data stack possible
- `edxapp` — highest complexity: MariaDB engine, multiple K8s sub-modules
  (`k8s_resources.py`, `k8s_secrets.py`, `k8s_configmaps.py`, `k8s_autoscaling.py`),
  and downstream dependency from `edx_notes`

### Phase 5 — CI/CD updates

Update Concourse pipelines so:
- Data stacks deploy independently, triggered by changes to `data/__main__.py` or
  infrastructure-level events (engine version bumps, storage changes)
- Application stacks deploy on code changes without waiting for data stack operations
- Update deployment documentation and runbooks
