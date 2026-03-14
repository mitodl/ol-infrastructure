# 0007. Multi-Tenant ClickHouse Cluster for LLMOps on Data EKS

**Status:** Accepted
**Date:** 2026-02-25
**Deciders:** Platform Engineering Team
**Technical Story:** Deploy shared ClickHouse infrastructure to support LLMOps tooling (TensorZero, OpenLit, Opik) with bottomless storage semantics

## Context

### Current Situation

MIT Open Learning's data EKS cluster runs Dagster, Superset, Airbyte, and StarRocks. Multiple LLMOps tools (TensorZero for inference gateways, OpenLit and Opik for observability) require a high-performance analytical database. Each tool has its own ClickHouse dependency but none are currently deployed.

### Problem Statement

Running a separate ClickHouse cluster per LLMOps tool would be operationally expensive, difficult to maintain, and wasteful of compute resources. LLMOps tools generate write-heavy telemetry workloads (spans, traces, metrics) that are well-suited for ClickHouse's merge-tree engine but require durable, cost-effective long-term storage.

### Business/Technical Drivers

- LLMOps telemetry data grows unboundedly; cost-effective cold storage is required
- Platform team operates a small number of EKS clusters; adding a new cluster per tool is unsustainable
- The data EKS cluster already has the monitoring, secrets, and networking infrastructure needed
- ClickHouse's columnar engine handles analytical queries across large telemetry datasets efficiently

### Constraints

- Must use the existing Altinity operator pattern (Apache 2.0, mature, production-grade)
- Must integrate with the existing Vault + VSO secrets management pattern
- Must use IRSA for S3 access (no long-lived AWS credentials)
- Must support multi-tenancy: each LLMOps tool gets its own database and user

### Assumptions

- The data EKS cluster has sufficient capacity to add the io-optimized node group
- LLMOps tools (TensorZero, OpenLit, Opik) will be deployed in the data EKS cluster (not the applications cluster) to keep write-heavy traffic intra-cluster

## Decision

### Operator Choice: Altinity ClickHouse Operator v0.26.0

**Chosen Option:** Altinity operator (not ClickHouse Inc. operator)

**Rationale:**
| Factor | Altinity (chosen) | ClickHouse Inc. |
|--------|-------------------|-----------------|
| License | Apache 2.0 | Apache 2.0 |
| Maturity | Production since 2019, v0.26.0 | v0.0.2, released Feb 2025 |
| Adoption | Thousands of production deployments | Essentially no production adoption |
| Requirements | None | Requires cert-manager |
| Install method | Single YAML bundle | Helm (cert-manager dep) |

The ClickHouse Inc. operator is too new to trust with production data. Altinity is the de facto standard.

### Cluster Topology: 1 Shard × 3 Replicas

**Chosen Option:** Single shard with 3 replicas (not 3 shards × 1 replica)

**Rationale:** A single shard means LLMOps tools can use standard SQL without distributed table setup. Replication provides HA without requiring tools to be replication-aware. Sharding can be added later if horizontal scaling is needed (ClickHouse supports resharding online).

### Storage: NVMe Hot + S3 Cold (Bottomless Storage)

**Chosen Option:** Native ClickHouse S3 tiered storage via disk policies

ClickHouse stores hot data on local NVMe SSD (i4i instance-store). After N days (configurable per environment), table TTL MOVE expressions move data to a cold S3 disk backed by an S3 Intelligent-Tiering bucket. The S3 data remains queryable via ClickHouse's S3 disk without any custom query logic on the LLMOps tools' side.

**Options considered:**

1. **Native S3 tiered storage (chosen):** Hot data on NVMe, cold on S3. Tools set `TTL date_col + INTERVAL N DAY TO VOLUME 'cold'`. No external pipeline. ClickHouse manages the movement.
   - Pros: Transparent to LLMOps clients, no ETL, leverages Iceberg-compatible S3 Intelligent-Tiering, fixed cost model
   - Cons: Tools must define TTL expressions in their table DDL; requires NVMe instances

2. **EBS only (rejected):** All data on EBS gp3. No tiering.
   - Pros: Simpler storage setup
   - Cons: EBS IOPS costs scale with write volume (unpredictable), 16k IOPS max vs 7.6M NVMe IOPS, no cost-effective cold layer

3. **External Iceberg pipeline (rejected):** Separate Spark/Flink job moves data to Iceberg tables in S3; read via Trino.
   - Pros: True Iceberg format
   - Cons: Requires running an additional query engine, complex operational overhead, breaks direct ClickHouse query semantics

### Node Group: io-optimized (i4i instance family)

Dedicated `io-optimized` node group with NVMe instance-store SSD, shared with StarRocks BE nodes. ClickHouse's merge-heavy workload saturates EBS IOPS at scale; NVMe provides 7.6M IOPS at fixed cost vs. EBS which charges per-IOPS.

| Environment | Instance | NVMe Capacity | Hot Data Days |
|-------------|----------|---------------|---------------|
| CI | m7i.xlarge (EBS) | N/A (100 GiB EBS) | 1 |
| QA | i4i.xlarge | 937 GiB | 3 |
| Production | i4i.2xlarge | 1.875 TiB | 7 |

### Multi-Tenancy: Separate Database + User per Tool

Each LLMOps tool gets:
- A dedicated database (`tensorzero_db`, `openlit_db`, `opik_db`)
- A dedicated user with access restricted to that database
- Resource quotas (`llmops_profile`/`llmops_quota`) preventing one tenant from starving others

Passwords are stored in Vault KV (`secret-clickhouse/credentials`), synced to a `users.xml` K8s Secret via VSO with SHA256-hashed passwords (plaintext never leaves Vault), and volume-mounted into ClickHouse pods at `/etc/clickhouse-server/users.d/`.

### Cross-Cluster Access

LLMOps tools are deployed in the data EKS cluster (not the applications cluster). This keeps write-heavy ClickHouse traffic intra-cluster, avoiding:
- Cross-cluster network egress costs
- Latency from application cluster → data cluster → ClickHouse
- The complexity of PrivateLink or ExternalName services for high-throughput writes

Application-cluster services call the lightweight LLMOps HTTP APIs (not ClickHouse directly) through Application Load Balancers.

## Consequences

### Positive Consequences

- Single ClickHouse cluster serves all current and future LLMOps tools (multi-tenant)
- Native S3 tiering provides effectively unlimited historical data retention at low cost
- NVMe storage eliminates EBS IOPS bottleneck for merge-heavy ClickHouse workloads
- Vault + IRSA secrets integration is consistent with all other data-cluster applications
- Altinity operator provides HA, rolling upgrades, and schema migration tooling

### Negative Consequences

- i4i instances cost ~5% more than equivalent EBS-backed m7i instances on reserved pricing
- NVMe is ephemeral: node failure loses the hot data on that node (mitigated by 3-replica replication)
- LLMOps tools must define `TTL … TO VOLUME 'cold'` in their table DDL to participate in tiering
- CI environment uses EBS (not NVMe) for cost reasons — slight behavioral difference in CI vs. QA/Prod

### Neutral Consequences

- StarRocks BE nodes gain the same io-optimized node group benefit (shared infrastructure)
- Keeper coordination pods run on the same io-optimized nodes (Keeper itself benefits minimally from NVMe, but co-location avoids scheduling complexity)

## Implementation Notes

**Deployment order:**
1. `infrastructure.aws.eks.data.*` — adds namespaces and io-optimized node group
2. `substructure.aws.eks.data.*` — installs Altinity operator + NVMe setup DaemonSet + local-path-provisioner
3. `substructure.vault.static_mounts.operations.*` — creates Vault KV mount for ClickHouse
4. `applications.clickhouse.*` — deploys S3 bucket, IRSA, Keeper, ClickHouseInstallation, Vault KV secrets, VSO sync

**Post-deploy manual steps:**
```sql
-- Connect as admin and create per-tool databases
CREATE DATABASE IF NOT EXISTS tensorzero_db;
CREATE DATABASE IF NOT EXISTS openlit_db;
CREATE DATABASE IF NOT EXISTS opik_db;
```

**Risk Level:** Medium — new node group and operator; all other patterns are established

**Dependencies:** local-path-provisioner must be healthy before ClickHouseInstallation is created (QA/Prod only)

## Related Decisions

- [ADR-0005](0005-high-performance-stateful-applications-eks.md) — High-Performance Stateful Applications in EKS (NVMe node group rationale)
- [ADR-0002](0002-migrate-to-gateway-api-httproute.md) — Gateway API HTTPRoute (used for LLMOps tool external access)

## References

- [Altinity ClickHouse Operator](https://github.com/Altinity/clickhouse-operator)
- [ClickHouse S3 Tiered Storage](https://clickhouse.com/docs/en/integrations/s3)
- [Rancher local-path-provisioner](https://github.com/rancher/local-path-provisioner)
- [i4i Instance Family](https://aws.amazon.com/ec2/instance-types/i4i/)

---

**Review History:**

| Date | Reviewer | Decision | Notes |
|------|----------|----------|-------|
| 2026-02-25 | Platform Engineering | Accepted | Initial implementation |

**Last Updated:** 2026-02-25
