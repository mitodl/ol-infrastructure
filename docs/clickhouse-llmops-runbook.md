# ClickHouse LLMOps Runbook

This runbook covers operational procedures for the multi-tenant ClickHouse cluster
deployed on the data EKS cluster for LLMOps tooling (TensorZero, Opik).

**Architecture summary:** See [ADR-0007](adr/0007-clickhouse-llmops-multi-tenant.md)

**Pulumi stacks:**
- `infrastructure.aws.eks.data.<env>` — EKS cluster, namespaces, io-optimized node group
- `substructure.aws.eks.data.<env>` — Altinity operator, NVMe setup, local-path-provisioner
- `substructure.vault.static_mounts.operations.<env>` — Vault KV mount (`secret-clickhouse`)
- `applications.clickhouse.<env>` — S3 bucket, IRSA, Keeper, ClickHouseInstallation

---

## Table of Contents

1. [Initial Cluster Setup](#initial-cluster-setup)
2. [Adding a New LLMOps Tenant](#adding-a-new-llmops-tenant)
3. [Rotating Credentials](#rotating-credentials)
4. [Adjusting Hot/Cold Storage Tier Cutoff](#adjusting-hotcold-storage-tier-cutoff)
5. [Scaling the Cluster](#scaling-the-cluster)
6. [Monitoring and Alerting](#monitoring-and-alerting)
7. [Backup and Restore](#backup-and-restore)
8. [Troubleshooting](#troubleshooting)
9. [Useful Commands](#useful-commands)

---

## Initial Cluster Setup

After deploying the Pulumi stacks, the per-tool databases must be created manually
(there is no ClickHouse Pulumi provider for DDL).

```bash
# Port-forward to the ClickHouse HTTP port
kubectl port-forward -n clickhouse svc/clickhouse 8123:8123 &

# Get admin password from Vault
ADMIN_PASSWORD=$(vault kv get -field=admin secret-clickhouse/credentials)

# Create per-tool databases
curl -s "http://localhost:8123/?user=admin&password=$ADMIN_PASSWORD" \
  --data "CREATE DATABASE IF NOT EXISTS tensorzero_db"
curl -s "http://localhost:8123/?user=admin&password=$ADMIN_PASSWORD" \
  --data "CREATE DATABASE IF NOT EXISTS opik_db"

# Verify
curl -s "http://localhost:8123/?user=admin&password=$ADMIN_PASSWORD" \
  --data "SHOW DATABASES"
```

---

## Adding a New LLMOps Tenant

To onboard a new tool (e.g., `langfuse`):

### 1. Update the Vault KV secret and users.xml template

Edit `src/ol_infrastructure/applications/clickhouse/__main__.py`:

a. Add the password config read:
```python
langfuse_password = clickhouse_config.get_secret("langfuse_password") or Output.secret(
    "changeme"
)
```

b. Add to the `vault.kv.SecretV2` data_json:
```python
.apply(json.dumps)  # add langfuse=langfuse_password to the Output.all()
```

c. Add the user block to the operator-native `ch_users` map:
```python
"langfuse/password_sha256_hex": _secret_ref("langfuse_sha256"),
"langfuse/profile": "llmops_profile",
"langfuse/quota": "llmops_quota",
"langfuse/networks/ip": ["::/0", "0.0.0.0/0"],
"langfuse/grants/query": [
    "GRANT ALL ON langfuse_db.*",
],
```

Prefer explicit `grants/query` entries over the legacy `allow_databases/database`
whitelist. `allow_databases` restricts the user's RBAC grants to *only* the listed
application databases, which denies access to any `system.*` table. If the tool
queries `system.parts` (or other system tables) — as opik does for storage
metrics — add a targeted `"GRANT SELECT ON system.parts"` rather than exposing the
whole `system` database, so cross-tenant isolation (e.g. `system.query_log`) is
preserved.

d. Add `"langfuse"` to the `LLMOPS_NAMESPACES` list for NetworkPolicy.

### 2. Set the password in each environment

```bash
cd src/ol_infrastructure/applications/clickhouse/
pulumi stack select applications.clickhouse.Production
pulumi config set --secret clickhouse:langfuse_password <generated-password>
```

### 3. Deploy

```bash
pulumi up
```

### 4. Create the database

```bash
kubectl port-forward -n clickhouse svc/clickhouse 8123:8123
ADMIN_PASSWORD=$(vault kv get -field=admin secret-clickhouse/credentials)
curl -s "http://localhost:8123/?user=admin&password=$ADMIN_PASSWORD" \
  --data "CREATE DATABASE IF NOT EXISTS langfuse_db"
```

---

## Rotating Credentials

### Rotate a per-tool password

```bash
# Generate a new password
NEW_PASS=$(openssl rand -base64 32)

# Set in Pulumi config (all environments)
for env in CI QA Production; do
  cd src/ol_infrastructure/applications/clickhouse/
  pulumi stack select applications.clickhouse.$env
  pulumi config set --secret clickhouse:tensorzero_password "$NEW_PASS"
done

# Deploy — VSO will sync the new users.xml within the refresh_after window (1h)
pulumi up

# Force immediate VSO sync if needed
kubectl annotate vaultstaticsecret -n clickhouse clickhouse-users-config \
  vault.hashicorp.com/requestedAt="$(date -u +%Y-%m-%dT%H:%M:%SZ)" --overwrite
```

### Rotate the admin password

Same process but for `clickhouse:admin_password`. The admin password change takes
effect when ClickHouse reloads the users.xml (happens automatically within ~60s
of VSO syncing the new secret).

---

## Adjusting Hot/Cold Storage Tier Cutoff

The `hot_data_days` config is **informational only** — it is stored as a Pulumi stack
config value for operator reference but is **not read by the Pulumi program** and does
not automatically configure ClickHouse TTL MOVE intervals. The actual data movement
cutoff is determined by the `TTL … TO VOLUME 'cold'` expressions defined in each
table's DDL.

Each LLMOps tool must define `TTL` expressions in their table DDL pointing to the
`tiered` storage policy. Example table DDL:
```sql
CREATE TABLE IF NOT EXISTS tensorzero_db.spans
(
    span_id UUID,
    timestamp DateTime,
    -- ... other columns
)
ENGINE = ReplicatedMergeTree('/clickhouse/tables/{shard}/tensorzero_db.spans', '{replica}')
PARTITION BY toYYYYMM(timestamp)
ORDER BY (timestamp, span_id)
TTL timestamp + INTERVAL 7 DAY TO VOLUME 'cold'
SETTINGS storage_policy = 'tiered';
```

To update the cutoff for a table, alter the TTL expression directly in ClickHouse:
```sql
ALTER TABLE tensorzero_db.spans MODIFY TTL timestamp + INTERVAL 14 DAY TO VOLUME 'cold';
```

Updating `clickhouse:hot_data_days` via `pulumi config set` and running `pulumi up`
will **not** change the TTL of existing tables; it only records the intended policy for
operator awareness.

---

## Scaling the Cluster

### Vertical scaling (larger instance type)

1. Update `instance_type` in `Pulumi.infrastructure.aws.eks.data.Production.yaml` under the `io-optimized` node group
2. `pulumi up` on `infrastructure.aws.eks.data.Production` — rolls the node group
3. ClickHouse StatefulSet pods will be rescheduled; Keeper maintains quorum during rolling restart

### Horizontal scaling (add a shard)

Currently the cluster runs 1 shard × 3 replicas. Adding a shard requires:

1. Update `shardsCount` in the ClickHouseInstallation CRD in `__main__.py`
2. Deploy new shard nodes; operator creates them automatically
3. LLMOps tools must update table DDL to use `Distributed` tables or sharding keys
   (this is a breaking change for existing tables)

Sharding is only recommended when a single shard exceeds ~2 TB hot data.

### Reducing hot storage tier size

If NVMe is running low, reduce `hot_data_days` (forces earlier eviction to S3).
Monitor via the `system.disks` query:
```sql
SELECT name, path, free_space, total_space
FROM system.disks
FORMAT PrettyCompact;
```

---

## Monitoring and Alerting

### Grafana dashboards

The `clickhouse` ServiceMonitor scrapes metrics from port 8123 at `/metrics` every 30s.
Useful PromQL queries:

```promql
# Disk usage percentage (hot NVMe)
100 - (clickhouse_disks_free_bytes{disk="hot_local"} / clickhouse_disks_total_bytes{disk="hot_local"} * 100)

# Replication queue depth (should stay near 0)
clickhouse_ReplicasMaxQueueSize

# Merge backlog (if high, writes may slow)
clickhouse_BackgroundMergesAndMutationsPoolTask

# Query latency p99 (seconds)
histogram_quantile(0.99, rate(clickhouse_query_duration_milliseconds_bucket[5m])) / 1000
```

### Key alerts to configure

| Alert | Condition | Severity |
|-------|-----------|----------|
| ClickHouseHotDiskUsageHigh | hot_local disk > 70% full | warning |
| ClickHouseHotDiskUsageCritical | hot_local disk > 85% full | critical |
| ClickHouseReplicationLag | ReplicasMaxQueueSize > 1000 for 10m | warning |
| ClickHouseKeeperDown | Keeper pod not ready | critical |
| ClickHousePodRestart | Pod restart count > 2 in 1h | warning |

---

## Backup and Restore

There are two recovery layers. Neither covers the other's case.

| Layer | What | Schedule / retention | Use it for |
|---|---|---|---|
| AWS Backup | EBS snapshot of every CH and Keeper PVC (`<cluster>-eks-backup-plan`, `infrastructure/aws/eks/aws_backup.py`) | Daily 05:00 UTC, 14 days | A lost or corrupted PVC. Crash-consistent and per volume, not coordinated across replicas. |
| SQL `BACKUP` | `clickhouse-backup` CronJob: every database except `system` / `INFORMATION_SCHEMA`, to `s3://ol-data-clickhouse-backup-<env>/backups/<UTC timestamp>/` | Daily 03:30 UTC, 14 days (`clickhouse:backup_retention_days`); overwritten or deleted objects kept 7 more days as noncurrent versions | Restoring tables or whole databases, into this cluster or another one at the same or a newer server version. |

The SQL backup runs on replica 0 (`chi-clickhouse-default-0-0`) because Opik's
Liquibase ledger (`default.DATABASECHANGELOG*`) exists only there. The target is
the `backups` disk (`s3_plain`), the only disk `backups.allowed_disk` permits.
The cold-tier bucket is not a backup: once tiering is active it holds live
parts.

### Check the latest backup

```bash
kubectl -n clickhouse get jobs -l app.kubernetes.io/name=clickhouse-backup
kubectl -n clickhouse logs job/<job-name>
aws s3 ls s3://ol-data-clickhouse-backup-<env>/backups/
```

`system.backups` only lists runs since that server last restarted, and only on
the server that ran them.

### Restore

Restore a single table next to the live one, then compare or swap:

```sql
RESTORE TABLE opik_db.spans AS opik_db.spans_restored
FROM Disk('backups', '<UTC timestamp>');
```

Restore a whole database into a cluster where it does not exist yet:

```sql
RESTORE DATABASE opik_db FROM Disk('backups', '<UTC timestamp>');
RESTORE TABLE default.DATABASECHANGELOG, TABLE default.DATABASECHANGELOGLOCK
FROM Disk('backups', '<UTC timestamp>');
```

Run the restore on replica 0. Replicated tables are recreated under their
original ZooKeeper paths, so restore a whole database only into a cluster that
does not already have those tables; for the live cluster, restore under a new
table name as above.

---

## Troubleshooting

### Keeper quorum lost

If Keeper pods are unhealthy:
```bash
# Check Keeper status
kubectl exec -n clickhouse clickhouse-keeper-0 -- \
  bash -c "echo ruok | nc localhost 2181"

# Check Keeper logs
kubectl logs -n clickhouse clickhouse-keeper-0 --tail=100

# Force pod restart (Keeper uses Raft; majority must be healthy before restart)
kubectl delete pod -n clickhouse clickhouse-keeper-2
```

### ClickHouseInstallation not ready

```bash
# Check operator logs
kubectl logs -n clickhouse-operator -l app=clickhouse-operator --tail=50

# Check CHI status
kubectl get chi -n clickhouse clickhouse -o yaml | grep -A20 status:

# Check pod events
kubectl describe pod -n clickhouse chi-clickhouse-default-0-0
```

### VSO not syncing users.xml

```bash
# Check VaultStaticSecret status
kubectl describe vaultstaticsecret -n clickhouse clickhouse-users-config

# Force sync
kubectl annotate vaultstaticsecret -n clickhouse clickhouse-users-config \
  vault.hashicorp.com/requestedAt="$(date -u +%Y-%m-%dT%H:%M:%SZ)" --overwrite

# Verify the secret contents (should contain users.xml)
kubectl get secret -n clickhouse clickhouse-users -o jsonpath='{.data.users\.xml}' | base64 -d
```

### NVMe not mounting (QA/Prod)

```bash
# Check NVMe init DaemonSet status
kubectl get pods -n kube-system -l app=nvme-init
kubectl logs -n kube-system -l app=nvme-init -c nvme-setup --previous

# Manually verify NVMe is mounted on a node
kubectl debug node/<node-name> -it --image=amazonlinux:2023 -- \
  chroot /host mountpoint /mnt/nvme
```

### local-path-provisioner StorageClass missing

```bash
# Check if StorageClass exists
kubectl get storageclass local-nvme

# Check provisioner pods
kubectl get pods -n kube-system -l app=local-path-provisioner

# Re-deploy via Pulumi
cd src/ol_infrastructure/substructure/aws/eks/
pulumi stack select substructure.aws.eks.data.Production
pulumi up --target '*local-path-provisioner*'
```

---

## Useful Commands

```bash
# Connect to ClickHouse with clickhouse-client
kubectl exec -it -n clickhouse chi-clickhouse-default-0-0 -- \
  clickhouse-client --user admin

# Check replication status across replicas
kubectl exec -it -n clickhouse chi-clickhouse-default-0-0 -- \
  clickhouse-client --query "SELECT * FROM system.replicas FORMAT Vertical" --user admin

# Check disk usage
kubectl exec -it -n clickhouse chi-clickhouse-default-0-0 -- \
  clickhouse-client --query "SELECT name, path, formatReadableSize(free_space), formatReadableSize(total_space) FROM system.disks" --user admin

# Check background merges
kubectl exec -it -n clickhouse chi-clickhouse-default-0-0 -- \
  clickhouse-client --query "SELECT * FROM system.merges FORMAT Vertical" --user admin

# Force data movement to cold tier (manual TTL enforcement)
kubectl exec -it -n clickhouse chi-clickhouse-default-0-0 -- \
  clickhouse-client --query "OPTIMIZE TABLE tensorzero_db.spans FINAL" --user admin

# Tail ClickHouse query log (useful for debugging slow queries)
kubectl exec -it -n clickhouse chi-clickhouse-default-0-0 -- \
  clickhouse-client --query "SELECT query, elapsed, read_rows FROM system.processes" --user admin
```
