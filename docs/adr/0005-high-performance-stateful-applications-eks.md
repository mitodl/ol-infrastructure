# 0005. High Performance Stateful Applications in EKS

**Status:** Proposed
**Date:** 2025-11-20
**Deciders:** Platform Team (pending approval)
**Technical Story:** Architecture decision for running stateful workloads on EKS

## Context

### Current Situation

MIT Open Learning's EKS infrastructure has:
- **3-5 'core nodes':** Stable on-demand instances for critical workloads
- **Karpenter-managed nodes:** Primarily spot instances for cost optimization
- **Available CSI drivers:** EBS (gp3) and EFS already installed
- **Node architecture:** Capability to create additional stable node classes outside Karpenter

Applications requiring high performance and stateful storage need architectural decisions that balance:
1. **Reliability/Stability** (primary priority) - data durability, node stability, failover guarantees
2. **Performance** (secondary priority) - IOPS, throughput, latency
3. **Other considerations** - cost, operational complexity, scalability

### Problem Statement

Stateful applications (databases, caches, analytics workloads, ML training) require persistent storage with specific characteristics:
- **High IOPS/throughput** for performance-sensitive operations
- **Data durability** to prevent loss during node failures
- **Pod portability** or lack thereof depending on storage type
- **Node stability** to minimize disruption and data migration

Current infrastructure can support multiple patterns, but we lack a documented standard approach for where to run stateful workloads and which storage backend to use.

### Business/Technical Drivers

- **Reliability First:** Stateful applications often hold critical data - minimize risk of data loss or corruption
- **Performance Requirements:** Some workloads (databases, real-time analytics) require low-latency, high-IOPS storage
- **Cost Efficiency:** Spot instances reduce costs but increase disruption risk
- **Operational Simplicity:** Prefer patterns that minimize operational overhead and failure scenarios
- **Future Growth:** Architecture should scale to additional stateful applications

### Constraints

- **MAY** use existing EKS clusters and CSI drivers (EBS gp3, EFS) but additional storage classes are permitted (EBS io2, io2 Block Express)
- **MAY** work with current Karpenter configuration (spot-heavy) - can provision non-Karpenter nodes as well
- **Should not** require major changes to cluster architecture
- **Must** support pod rescheduling for maintenance/updates
- **Should** minimize changes to existing application deployment patterns

### Assumptions

- EBS snapshots and backups are configured at infrastructure level
- Monitoring and alerting exist for node and storage health
- Applications follow best practices for stateful workload design
- Team has capacity to manage additional node groups if needed

## Options Considered

### Option 1: Dedicated Stable Node Pool + EBS gp3 (Recommended)

**Approach:** Create a dedicated stable (non-Karpenter, on-demand) node pool specifically for stateful workloads, using EBS gp3 volumes via CSI driver.

**Architecture:**
- Label/taint nodes: `workload-type=stateful:NoSchedule`
- StatefulSets with:
  - Node affinity to target stable nodes
  - EBS gp3 PersistentVolumeClaims
  - Pod topology spread for AZ distribution
- EBS volumes pinned to specific AZs (pods must schedule to same AZ as volume)

**Pros:**
- ✅ **Highest reliability:** On-demand nodes eliminate spot termination risk
- ✅ **Excellent performance:** EBS gp3 provides consistent IOPS (3,000-16,000) and throughput (125-1,000 MiB/s)
- ✅ **Data durability:** EBS volumes replicated within AZ (99.8-99.9% durability)
- ✅ **Proven pattern:** Industry standard for stateful workloads on EKS
- ✅ **Cost predictable:** Fixed node costs, pay for provisioned storage
- ✅ **Snapshot integration:** EBS snapshots for backups and disaster recovery
- ✅ **Performance tuning:** Can increase IOPS/throughput per volume independently

**Cons:**
- ❌ **Higher node cost:** On-demand pricing vs. spot (60-70% more expensive)
- ❌ **AZ coupling:** Pods tied to specific AZs (cannot cross-AZ failover without storage replication)
- ❌ **Additional node pool:** Operational complexity of managing separate node group
- ❌ **Pod rescheduling complexity:** Node maintenance requires draining pods that are tied to AZ-specific volumes

**Use Cases:**
- Databases (PostgreSQL, MySQL, MongoDB)
- Caching layers (Redis, Memcached with persistence)
- Analytics workloads requiring high IOPS
- ML training with large model checkpoints
- Any application where data loss is unacceptable

**Implementation:**
```yaml
# Node pool configuration
nodeSelector:
  workload-type: stateful
tolerations:
- key: "workload-type"
  operator: "Equal"
  value: "stateful"
  effect: "NoSchedule"

# Storage configuration
volumeClaimTemplates:
- metadata:
    name: data
  spec:
    accessModes: ["ReadWriteOnce"]
    storageClassName: ebs-gp3-high-performance
    resources:
      requests:
        storage: 100Gi
```

**Cost Estimate (per instance):**
- Node: r6i.xlarge on-demand ~$121/month
- Storage: 100GB EBS gp3 ~$8/month + IOPS/throughput if provisioned above baseline
- Total: ~$130/month for moderately sized stateful workload

---

### Option 2: Core Nodes + EBS gp3

**Approach:** Use existing 3-5 core stable nodes for stateful workloads via node selectors, with EBS gp3 storage.

**Architecture:**
- Target existing core nodes with labels (e.g., `node-role=core`)
- StatefulSets configured to prefer/require core nodes
- Share node capacity with other critical workloads
- EBS gp3 volumes for storage

**Pros:**
- ✅ **No new infrastructure:** Leverages existing stable nodes
- ✅ **Zero additional node cost:** No new node pool required
- ✅ **High reliability:** Core nodes are on-demand and stable
- ✅ **Good performance:** EBS gp3 same as Option 1
- ✅ **Fast implementation:** No new node pool provisioning

**Cons:**
- ❌ **Resource contention:** Stateful apps compete with other core workloads for CPU/memory
- ❌ **Limited capacity:** Only 3-5 nodes available, constrains number of stateful workloads
- ❌ **Noisy neighbor risk:** Database workloads could impact other critical services
- ❌ **Scaling limitations:** Cannot scale stateful capacity independently from core infrastructure
- ❌ **AZ coupling:** Same EBS limitations as Option 1

**Use Cases:**
- Small number of stateful applications (1-3)
- Development/staging environments
- Low-resource stateful workloads (small Redis caches)
- POC/testing before creating dedicated node pool

**Implementation:**
```yaml
nodeSelector:
  node-role: core
# Same storage config as Option 1
```

**Cost Estimate:**
- Node: $0 incremental (existing)
- Storage: Same as Option 1 (~$8/100GB)
- Total: ~$8/month storage cost only

---

### Option 3: Karpenter Nodes with On-Demand Requirement + EBS gp3

**Approach:** Let Karpenter provision nodes for stateful workloads, but require on-demand instances via NodePool configuration.

**Architecture:**
- Create Karpenter NodePool with:
  - `capacity-type: on-demand` requirement
  - Suitable instance types for stateful workloads
  - Labels to identify nodes for stateful workloads
- StatefulSets use nodeSelector to target this NodePool
- Karpenter handles scaling/consolidation but only with on-demand

**Pros:**
- ✅ **Automatic scaling:** Karpenter provisions nodes as needed
- ✅ **Stability:** On-demand requirement eliminates spot termination
- ✅ **Bin packing:** Karpenter optimizes node utilization
- ✅ **Good performance:** EBS gp3 same as Options 1 & 2
- ✅ **Lower operational overhead:** Karpenter handles lifecycle
- ✅ **Flexibility:** Can define multiple NodePools for different stateful workload types

**Cons:**
- ❌ **Node churn risk:** Karpenter consolidation could disrupt stateful pods during cost optimization
- ❌ **Complexity with StatefulSets:** Karpenter's aggressive consolidation conflicts with AZ-pinned volumes
- ❌ **Less predictable:** Node scaling decisions made by Karpenter (less control than dedicated pool)
- ❌ **Requires Karpenter configuration:** Need to tune consolidation settings to protect stateful workloads
- ❌ **AZ coupling:** Same EBS limitations as Options 1 & 2

**Use Cases:**
- Stateful workloads with variable resource needs
- Applications that can tolerate occasional rescheduling
- Teams comfortable with Karpenter behavior
- Multi-tenant environments with many small stateful apps

**Implementation:**
```yaml
# Karpenter NodePool
apiVersion: karpenter.sh/v1beta1
kind: NodePool
metadata:
  name: stateful-ondemand
spec:
  disruption:
    consolidationPolicy: WhenUnderutilized
    consolidateAfter: 10m  # Longer period to reduce churn
  template:
    spec:
      requirements:
      - key: karpenter.sh/capacity-type
        operator: In
        values: ["on-demand"]
      - key: workload-type
        operator: In
        values: ["stateful"]
      nodeClassRef:
        name: default
```

**Cost Estimate:**
- Node: Same as Option 1 (~$250/month per node)
- Storage: Same as Option 1 (~$8/100GB)
- Total: ~$260/month, but with more dynamic scaling

---

### Option 4: EFS for High-Availability Stateful Workloads

**Approach:** Use EFS (already installed) for stateful applications that prioritize availability over raw performance.

**Architecture:**
- EFS file system mounted to pods via CSI driver
- Pods can run on any node (including Karpenter spot instances)
- `ReadWriteMany` access mode allows multi-pod access
- No AZ pinning - pods freely reschedule

**Pros:**
- ✅ **Maximum availability:** Pods can reschedule to any node/AZ without storage constraints
- ✅ **Shared storage:** Multiple pods can read/write simultaneously
- ✅ **Elastic capacity:** Storage scales automatically, no provisioning
- ✅ **Cross-AZ replication:** Data automatically replicated across AZs (highest durability)
- ✅ **Works with spot:** Pods can run on Karpenter spot nodes without stability concerns
- ✅ **No AZ coupling:** True portability across cluster

**Cons:**
- ❌ **Lower performance:** EFS latency (single-digit ms) vs. EBS (sub-ms)
- ❌ **Throughput limits:** Max 10 GB/s vs. EBS's per-volume throughput
- ❌ **Higher cost:** EFS Standard ~$0.30/GB/month vs. gp3 $0.08/GB/month (3.75x more expensive)
- ❌ **Not suitable for databases:** High latency makes it poor fit for OLTP databases
- ❌ **IOPS variability:** Credits-based system can throttle under load
- ❌ **Eventual consistency:** NFS semantics, not ideal for databases requiring strong consistency

**Use Cases:**
- Application logs and shared data
- Content management systems (WordPress, Drupal)
- ML training data that's read by multiple pods
- Configuration files shared across pods
- Low-write, high-read workloads
- Applications designed for NFS semantics

**Implementation:**
```yaml
volumeClaimTemplates:
- metadata:
    name: data
  spec:
    accessModes: ["ReadWriteMany"]
    storageClassName: efs-sc
    resources:
      requests:
        storage: 100Gi  # Soft limit, EFS is elastic
```

**Cost Estimate:**
- Node: Can use Karpenter spot (~$100/month per node, 60% savings)
- Storage: 100GB EFS ~$30/month (vs. $8 for EBS)
- Total: ~$130/month (lower node cost, higher storage cost)

---

### Option 5: Hybrid - EBS for Performance + EFS for Shared Data

**Approach:** Combine Options 1 and 4 - use EBS gp3 on stable nodes for performance-critical stateful workloads, and EFS for shared data and HA workloads.

**Architecture:**
- **Tier 1 (Critical):** Databases, high-IOPS apps → Dedicated stable nodes + EBS gp3
- **Tier 2 (Shared/HA):** CMS, shared storage, HA apps → Any node + EFS
- Document decision tree for teams to choose appropriate tier

**Pros:**
- ✅ **Best of both worlds:** Performance where needed, availability where needed
- ✅ **Flexibility:** Right tool for right job
- ✅ **Cost optimization:** Don't pay for stable nodes when not needed
- ✅ **Clear patterns:** Two well-defined approaches for different use cases

**Cons:**
- ❌ **Increased complexity:** Two patterns to maintain and document
- ❌ **Decision overhead:** Teams must choose which pattern to use
- ❌ **Operational burden:** More configurations to manage
- ❌ **Risk of wrong choice:** Teams might pick suboptimal pattern

**Use Cases:**
- Large platforms with diverse stateful workload needs
- Organizations with both OLTP databases and shared file storage needs
- Teams with capacity to document and support multiple patterns

**Implementation:**
Decision tree documentation:
```
Do you need sub-millisecond latency? → Yes → EBS gp3 + Stable Nodes
Do you need multi-pod write access? → Yes → EFS
Do you need >10,000 IOPS? → Yes → EBS gp3 + Stable Nodes
Is data shared across many pods? → Yes → EFS
Database workload? → Yes → EBS gp3 + Stable Nodes
Otherwise → Start with EFS, migrate to EBS if performance insufficient
```

**Cost Estimate:**
- Mixed: Depends on workload distribution
- Typical: ~$200/month (blend of both approaches)

---

### Option 6: Dedicated Stable Node Pool + EBS io2 Block Express (Ultra High Performance)

**Approach:** Similar to Option 1 but using EBS io2 Block Express for extreme performance requirements, targeting workloads that need >64,000 IOPS or sub-millisecond latency at scale.

**Architecture:**
- Same dedicated stable node pool as Option 1
- EBS io2 Block Express volumes (up to 256,000 IOPS, 4,000 MiB/s throughput)
- Requires r5b, r6ib, or x2idn instance families with EBS optimization
- StatefulSets with similar configuration to Option 1

**Pros:**
- ✅ **Extreme performance:** Up to 256,000 IOPS and 4,000 MiB/s per volume
- ✅ **Sub-millisecond latency:** Consistent <0.25ms latency (4x better than gp3)
- ✅ **Higher durability:** 99.999% durability (vs. 99.8-99.9% for gp3)
- ✅ **Multi-attach capable:** Single volume can attach to multiple EC2 instances (advanced use cases)
- ✅ **Best reliability:** Same stable node benefits as Option 1
- ✅ **Provisioned IOPS:** Guaranteed performance, no baseline/burst credits

**Cons:**
- ❌ **Significantly higher cost:** ~$0.125/GB/month + $0.065/IOPS/month (vs. gp3 $0.08/GB)
- ❌ **Cost scales with performance:** 64,000 IOPS = $4,160/month IOPS cost alone
- ❌ **Limited instance types:** Requires specific EBS-optimized instance families
- ❌ **Overkill for most workloads:** gp3's 16,000 IOPS sufficient for 95% of databases
- ❌ **Complex cost modeling:** Need to precisely estimate IOPS requirements

**Use Cases:**
- **Mission-critical databases** with >100,000 transactions/sec
- **High-frequency trading** or financial systems requiring <0.5ms latency
- **Large-scale analytics** (Clickhouse, TimescaleDB) with extreme IOPS needs
- **SAP HANA** or other in-memory databases with high I/O
- **Only when profiling proves gp3 is insufficient**

**Implementation:**
```yaml
# Storage configuration (same node config as Option 1)
volumeClaimTemplates:
- metadata:
    name: data
  spec:
    accessModes: ["ReadWriteOnce"]
    storageClassName: ebs-io2-block-express
    resources:
      requests:
        storage: 100Gi
    # Note: IOPS specified in StorageClass or via annotation
```

**Cost Estimate (per instance):**
- Node: r6ib.xlarge on-demand ~$334/month (EBS-optimized)
- Storage: 500GB io2 Block Express = $62.50/month
- IOPS: 64,000 provisioned = $4,160/month
- **Total: ~$4,556/month** (15x more than Option 1 with gp3)

**Decision:** Use only when profiling proves gp3 (16,000 IOPS max) is a bottleneck. Start with Option 1 (gp3) and upgrade if needed.

---

### Option 7: Spot Nodes with Frequent Snapshots + EBS gp3 (Not Recommended)

**Approach:** Run stateful workloads on Karpenter spot instances with aggressive EBS snapshot schedules to protect against data loss.

**Architecture:**
- Allow stateful workloads on spot instances
- Automated snapshots every 1-6 hours
- Accept higher disruption rate in exchange for cost savings

**Pros:**
- ✅ **Lowest cost:** Spot pricing (60-70% savings on compute)
- ✅ **EBS performance:** Same as Options 1-3
- ✅ **Snapshot protection:** Frequent backups reduce data loss window

**Cons:**
- ❌ **Reliability risk:** Spot terminations cause service disruptions
- ❌ **Data loss window:** 1-6 hours of data at risk between snapshots
- ❌ **Complexity:** Snapshot orchestration and recovery procedures
- ❌ **Slow recovery:** Restoring from snapshots takes time (minutes to hours)
- ❌ **Operational burden:** More incidents, more recovery operations
- ❌ **Not suitable for production:** Violates priority #1 (reliability/stability)

**Use Cases:**
- Development/test environments only
- Non-critical analytics where data can be regenerated
- POC workloads
- Cost-constrained environments accepting higher risk

**Implementation:**
```yaml
# Allow spot nodes (not recommended for production)
nodeSelector:
  karpenter.sh/capacity-type: spot
tolerations:
- key: "karpenter.sh/capacity-type"
  operator: "Equal"
  value: "spot"

# Aggressive snapshot schedule
# DLM policy: hourly snapshots, retain 24 hours
```

**Cost Estimate:**
- Node: r6i.xlarge spot ~$100/month (60% savings)
- Storage: $8/month + snapshot costs ~$5/month
- Total: ~$113/month
- **Hidden cost:** Incident response, data recovery operations

---

## Decision

**Chosen Option: Option 1 - Dedicated Stable Node Pool + EBS gp3**

### Rationale

Option 1 best aligns with the stated priority order (reliability > performance > other):

**1. Reliability/Stability (Primary Priority):**
- ✅ On-demand nodes eliminate spot termination risk
- ✅ EBS gp3 provides 99.8-99.9% durability within AZ
- ✅ Dedicated nodes prevent resource contention and noisy neighbor issues
- ✅ Industry-proven pattern for production stateful workloads

**2. Performance (Secondary Priority):**
- ✅ EBS gp3 baseline: 3,000 IOPS and 125 MiB/s throughput
- ✅ Can scale to 16,000 IOPS and 1,000 MiB/s per volume
- ✅ Sub-millisecond latency (far superior to EFS)
- ✅ Consistent performance (not credits-based)
- ✅ **Upgrade path to io2 Block Express if profiling proves gp3 insufficient**

**3. Other Considerations:**
- ✅ Scales independently from core infrastructure (vs. Option 2)
- ✅ More predictable than Karpenter-managed nodes (vs. Option 3)
- ✅ Lower operational burden than hybrid approach (vs. Option 5)
- ✅ Cost is higher but justified by reliability requirements
- ✅ **Flexibility:** Non-Karpenter nodes allowed per updated constraints

### Why Not Other Options?

**Option 2 (Core Nodes):** Limited capacity, resource contention risks, cannot scale independently

**Option 3 (Karpenter On-Demand):** Karpenter consolidation creates churn risk for AZ-pinned volumes, less predictable

**Option 4 (EFS):** Lower performance makes it unsuitable for high-performance databases and analytics

**Option 5 (Hybrid):** Adds operational complexity; can adopt later if diverse needs emerge

**Option 6 (io2 Block Express):** 15x higher cost ($4,500+ vs. $300/month); use as upgrade path, not starting point

**Option 7 (Spot):** Violates primary priority (reliability/stability), only suitable for dev/test

### Key Implementation Details

**Phase 1: Infrastructure Setup** (8 hours)
1. Create new Karpenter NodePool or ASG for stateful nodes
2. Configure node labels/taints: `workload-type=stateful:NoSchedule`
3. Define instance types (memory-optimized or storage-optimized based on workload)
4. Create EBS gp3 StorageClass with appropriate IOPS/throughput defaults

**Phase 2: Documentation** (4 hours)
1. Document when to use stateful node pool vs. other options
2. Create StatefulSet examples and best practices
3. Document AZ-awareness and scheduling constraints
4. Create runbook for node maintenance and draining

**Phase 3: Pilot Application** (8 hours)
1. Migrate one stateful application to new node pool
2. Validate performance meets requirements
3. Test failure scenarios (node drain, pod rescheduling)
4. Measure and baseline metrics

**Phase 4: Rollout** (ongoing)
- Migrate existing stateful workloads as needed
- Onboard new stateful applications to pattern
- Monitor and tune node capacity

### Exception Cases

**Use EFS when:**
- Multiple pods need simultaneous write access (ReadWriteMany)
- Application is designed for NFS semantics
- Availability is more important than performance
- Workload is shared file storage, not database

**Use Core Nodes when:**
- Only 1-2 small stateful applications
- POC/testing before creating dedicated pool
- Development environments

**Upgrade to io2 Block Express (Option 6) when:**
- Profiling shows sustained >12,000 IOPS utilization on gp3
- Latency SLOs require <0.5ms storage response times
- Budget approved for 10-15x storage cost increase
- Application team has exhausted query optimization, caching, read replicas

**DO NOT use Spot (Option 7) for production stateful workloads**

## Consequences

### Positive Consequences

- ✅ **High reliability:** Eliminates spot termination risk, dedicated capacity prevents contention
- ✅ **Predictable performance:** EBS gp3 provides consistent IOPS/throughput
- ✅ **Scalable architecture:** Can add nodes independently as stateful workloads grow
- ✅ **Clear separation:** Stateful workloads isolated from general-purpose workloads
- ✅ **Industry standard:** Well-documented pattern with extensive community knowledge
- ✅ **Backup integration:** EBS snapshots provide disaster recovery
- ✅ **Per-volume tuning:** Can optimize IOPS/throughput for each workload independently

### Negative Consequences

- ❌ **Higher cost:** On-demand nodes ~60-70% more expensive than spot
- ❌ **Additional node pool:** More infrastructure to manage and monitor
- ❌ **AZ coupling:** Pods tied to specific AZs, complicates cross-AZ failover
- ❌ **Node maintenance complexity:** Draining nodes with stateful pods requires coordination
- ❌ **Capacity planning:** Must size node pool appropriately (overprovisioning wastes money, underprovisioning causes scheduling failures)

### Neutral Consequences

- ⚪ **StatefulSet requirements:** Applications must use StatefulSets with proper PVC templates
- ⚪ **Documentation needs:** Team needs training on AZ-aware scheduling
- ⚪ **Monitoring changes:** Need dedicated dashboards for stateful node pool health

## Implementation Notes

### Effort Estimate

| Phase | Hours | Risk Level |
|-------|-------|-----------|
| Infrastructure setup | 8 | Low |
| Documentation | 4 | Low |
| Pilot application | 8 | Medium |
| **Total** | **20** | **Low-Medium** |

Ongoing: ~2 hours/month for capacity monitoring and optimization

### Risk Level

**Medium** - Dedicated node pool adds operational complexity, but pattern is well-proven

### Dependencies

- Karpenter or ASG configuration access
- EBS CSI driver (already installed)
- Monitoring/alerting for new node pool
- Budget approval for on-demand node costs

### Migration Path

**For new applications:**
1. Use StatefulSet with PVC template referencing `ebs-gp3` StorageClass
2. Add node selector: `workload-type: stateful`
3. Add tolerations for stateful taint
4. Deploy and validate
5. **Monitor performance** - if sustained >12,000 IOPS or latency SLOs not met, evaluate upgrade to io2

**For existing stateful applications (if any):**
1. Create new StatefulSet with correct node affinity
2. Blue-green migration: new pods on stable nodes, old pods on existing nodes
3. Migrate data (application-specific, may require backup/restore)
4. Cutover traffic to new pods
5. Delete old StatefulSet

**Performance upgrade path (gp3 → io2 Block Express):**
1. **Baseline metrics** on gp3: IOPS utilization, latency p50/p95/p99, throughput
2. **Identify bottleneck:** Use CloudWatch metrics to confirm storage is limiting factor (not CPU/memory/network)
3. **Optimize first:** Query tuning, indexes, caching, read replicas before throwing hardware at problem
4. **Create io2 StorageClass:** Define with required IOPS (start with 32,000, not max 256,000)
5. **Test in non-prod:** Validate performance improvement justifies 10-15x cost increase
6. **Gradual rollout:** Migrate most critical workload first, measure ROI before migrating others

### Technical Configuration

**StorageClass (gp3 - Default/Starting Point):**
```yaml
apiVersion: storage.k8s.io/v1
kind: StorageClass
metadata:
  name: ebs-gp3-high-performance
provisioner: ebs.csi.aws.com
parameters:
  type: gp3
  iops: "3000"        # Baseline, can override per PVC
  throughput: "125"    # Baseline, can override per PVC
  encrypted: "true"
volumeBindingMode: WaitForFirstConsumer  # Important: AZ-aware binding
allowVolumeExpansion: true
reclaimPolicy: Delete  # Or Retain for production databases
```

**StorageClass (io2 Block Express - Performance Upgrade):**
```yaml
apiVersion: storage.k8s.io/v1
kind: StorageClass
metadata:
  name: ebs-io2-block-express
provisioner: ebs.csi.aws.com
parameters:
  type: io2
  iops: "32000"       # Start conservative, scale up based on metrics
  throughput: "1000"   # MiB/s
  encrypted: "true"
  blockExpress: "true"  # Enables io2 Block Express features
volumeBindingMode: WaitForFirstConsumer
allowVolumeExpansion: true
reclaimPolicy: Retain  # Retain for critical databases
```

**Note on IOPS/Throughput:**
- gp3: Can provision up to 16,000 IOPS and 1,000 MiB/s
- io2 Block Express: Can provision up to 256,000 IOPS and 4,000 MiB/s
- **Start conservative:** Don't over-provision IOPS (costs $0.065/IOPS/month)
- **Monitor and scale:** Use CloudWatch to identify actual utilization before increasing

**StatefulSet Template:**
```yaml
apiVersion: apps/v1
kind: StatefulSet
metadata:
  name: postgres
spec:
  serviceName: postgres
  replicas: 1
  selector:
    matchLabels:
      app: postgres
  template:
    metadata:
      labels:
        app: postgres
    spec:
      # Node affinity for stable nodes
      affinity:
        nodeAffinity:
          requiredDuringSchedulingIgnoredDuringExecution:
            nodeSelectorTerms:
            - matchExpressions:
              - key: workload-type
                operator: In
                values:
                - stateful
        # Anti-affinity to spread across AZs (if replicas > 1)
        podAntiAffinity:
          preferredDuringSchedulingIgnoredDuringExecution:
          - weight: 100
            podAffinityTerm:
              labelSelector:
                matchLabels:
                  app: postgres
              topologyKey: topology.kubernetes.io/zone
      tolerations:
      - key: "workload-type"
        operator: "Equal"
        value: "stateful"
        effect: "NoSchedule"
      containers:
      - name: postgres
        image: postgres:15
        volumeMounts:
        - name: data
          mountPath: /var/lib/postgresql/data
  volumeClaimTemplates:
  - metadata:
      name: data
    spec:
      accessModes: ["ReadWriteOnce"]
      storageClassName: ebs-gp3-high-performance
      resources:
        requests:
          storage: 100Gi
```

### Success Criteria

**Technical:**
- ✅ Stateful node pool provisioned and healthy
- ✅ EBS volumes provision successfully with correct parameters
- ✅ Pods schedule to stable nodes only
- ✅ Performance meets application SLOs (latency, IOPS, throughput)
- ✅ EBS snapshots configured for disaster recovery

**Operational:**
- ✅ Zero data loss incidents
- ✅ <1% unplanned pod evictions (only during maintenance)
- ✅ Node pool capacity sufficient for planned workloads + 20% buffer
- ✅ Team trained on node draining procedures

## Related Decisions

- **ADR-0001:** Use ADR for Architecture Decisions
- **Future ADR:** May need separate decision for multi-AZ stateful workload patterns
- **Future ADR:** Backup and disaster recovery strategy for stateful workloads

## References

### AWS Documentation
- [EBS Volume Types](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/ebs-volume-types.html) - gp3, io2, io2 Block Express specifications
- [EBS io2 Block Express](https://aws.amazon.com/ebs/provisioned-iops/) - Performance details and pricing
- [EBS CSI Driver](https://github.com/kubernetes-sigs/aws-ebs-csi-driver) - CSI driver documentation
- [EKS Best Practices - Stateful Workloads](https://aws.github.io/aws-eks-best-practices/reliability/docs/dataplane/#stateful-workloads)

### Kubernetes Documentation
- [StatefulSets](https://kubernetes.io/docs/concepts/workloads/controllers/statefulset/)
- [Pod Topology Spread](https://kubernetes.io/docs/concepts/scheduling-eviction/topology-spread-constraints/)
- [Storage Classes](https://kubernetes.io/docs/concepts/storage/storage-classes/)

### Karpenter Documentation
- [NodePool Spec](https://karpenter.sh/docs/concepts/nodepools/)
- [Disruption Controls](https://karpenter.sh/docs/concepts/disruption/)

### Comparison Resources
- [EBS vs EFS Performance](https://aws.amazon.com/blogs/storage/comparing-amazon-ebs-and-amazon-efs/) - Official AWS comparison
- [gp3 vs io2 Decision Guide](https://aws.amazon.com/blogs/storage/amazon-ebs-gp3-vs-io2/) - When to use each volume type
- [Database on Kubernetes Best Practices](https://www.datadoghq.com/blog/kubernetes-databases/)
- [io2 Block Express Performance Analysis](https://aws.amazon.com/blogs/storage/deep-dive-on-amazon-ebs-io2-block-express-volumes/)

## Notes

### Cost Analysis

**Example workload:** PostgreSQL database requiring 4 vCPU, 16GB RAM, 500GB storage

**Option 1 (Chosen):**
- Node: r6i.xlarge on-demand (4 vCPU, 32GB) = $121/month
- Storage: 500GB gp3 = $40/month + $10/month for snapshots
- **Total: ~$171/month**

**Option 2 (Core Nodes):**
- Node: $0 incremental (existing)
- Storage: $50/month
- **Total: ~$50/month**
- **Trade-off:** Resource contention, limited capacity

**Option 3 (Karpenter On-Demand):**
- Same cost as Option 1 (~$302/month)
- **Trade-off:** Node churn risk during consolidation

**Option 4 (EFS):**
- Node: Can use spot = $100/month
- Storage: 500GB EFS Standard = $150/month
- **Total: ~$250/month**
- **Trade-off:** 10-20x worse latency, unsuitable for databases

**Option 6 (io2 Block Express):**
- Node: r6ib.xlarge on-demand = $334/month
- Storage: 500GB io2 Block Express = $62.50/month
- IOPS: 64,000 provisioned = $4,160/month
- **Total: ~$4,556/month**
- **Trade-off:** 15x cost increase vs. gp3, only for proven extreme performance needs

**Option 7 (Spot + Snapshots):**
- Node: r6i.xlarge spot = $100/month
- Storage: $50/month + $15/month for hourly snapshots
- **Total: ~$165/month**
- **Trade-off:** High disruption risk, data loss potential, NOT RECOMMENDED

**Recommendation:**
- Start with Option 1 (gp3) at $302/month - covers 95% of production database workloads
- Upgrade to Option 6 (io2 Block Express) at $4,556/month only if profiling proves bottleneck
- Option 1's $302/month is justified by eliminating reliability risks worth far more than $150-250/month in incident costs

### Performance Benchmarks

**EBS gp3 (Option 1, 2, 3):**
- Latency: <1ms (typically 0.5-0.8ms)
- IOPS: 3,000-16,000 (provisioned)
- Throughput: 125-1,000 MiB/s (provisioned)
- **Use case:** OLTP databases, high-IOPS applications (covers 95% of workloads)

**EBS io2 Block Express (Option 6):**
- Latency: <0.25ms (sub-millisecond at scale)
- IOPS: 3,000-256,000 (provisioned)
- Throughput: 1,000-4,000 MiB/s (provisioned)
- Durability: 99.999% (5 nines)
- **Use case:** Extreme performance databases, HFT, large-scale analytics, proven bottlenecks only

**EFS (Option 4):**
- Latency: 1-3ms (read), 5-10ms (write)
- IOPS: Credits-based, burstable
- Throughput: Up to 10 GB/s aggregate
- **Use case:** Shared file storage, CMS, low-write workloads

### Node Sizing Recommendations

**General purpose stateful (databases, caches):**
- r6i family (memory-optimized): r6i.xlarge, r6i.2xlarge
- m6i family (balanced): m6i.xlarge, m6i.2xlarge

**Storage-intensive (analytics, ML):**
- i4i family (NVMe SSD): i4i.xlarge, i4i.2xlarge
- Note: May still want EBS for durability, use instance storage for temp data

**Cost-sensitive:**
- r6a family (AMD, 10% cheaper): r6a.xlarge, r6a.2xlarge

### Open Questions

1. **How many stateful applications planned?** Affects node pool sizing
2. **What are the specific performance requirements?** May need higher IOPS/throughput provisioning (gp3 vs io2)
3. **Multi-AZ HA required?** May need replication strategy (future ADR)
4. **Existing stateful workloads?** Need migration plan if any exist
5. **Budget approval needed?** On-demand nodes increase costs vs. spot
6. **Any workloads currently bottlenecked on storage?** May justify immediate io2 Block Express evaluation

**Note on Updated Constraints:** The relaxed constraints (allowing additional storage classes and non-Karpenter nodes) enable **Option 6 (io2 Block Express)** as a viable upgrade path. However, the recommendation remains **Option 1 (gp3)** as the starting point for 95% of workloads, with io2 as a performance upgrade when profiling proves necessary.

---

**Review History:**

| Date | Reviewer | Decision | Notes |
|------|----------|----------|-------|
| 2025-11-20 | GitHub Copilot | Proposed | Created based on infrastructure analysis |
| _TBD_ | _Platform Team_ | _Pending_ | Needs approval for dedicated node pool creation |

**Last Updated:** 2025-11-20 (Updated constraints to allow additional storage classes and non-Karpenter nodes; added Option 6 for io2 Block Express)
