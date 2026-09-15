# Non-prod EBS volume right-sizing runbook

Shrinking the CI/QA typesense, StarRocks FE and ClickHouse hot volumes from the
100Gi/500Gi defaults to sizes matched to measured usage.

The Pulumi config change on its own **reclaims nothing** — and for typesense it is
actively hazardous if left unpaired with the recreate below. Read
[Why the config change is not enough](#why-the-config-change-is-not-enough) before
running `pulumi up` on any of these stacks.

## Measurement this is based on

`kubelet_volume_stats_used_bytes` / `kubelet_volume_stats_capacity_bytes`, taken
2026-09-15. CI figures scraped directly from each node's kubelet
(`/api/v1/nodes/<node>/proxy/metrics`) because neither Grafana org carries CI;
QA figures and all growth curves from the QA Grafana org over 90 days.

| Workload | Vols | Capacity | Used | 90-day trend | New size |
|---|---:|---:|---:|---|---:|
| typesense CI+QA | 24 | 100Gi | 0.74–0.86 GiB (0.8%) | flat, ~+0.02 GiB/mo | **20Gi** |
| starrocks FE meta, QA | 3 | 100Gi | 1.1–3.0 GiB | sawtooth, no trend | **20Gi** |
| starrocks FE log, QA | 3 | 100Gi | 1.3–5.1 GiB | sawtooth, no trend | **20Gi** |
| clickhouse hot, QA | 1 | 500Gi | 9.1 GiB (1.9%) | **+1.95 GiB/mo** | **100Gi** |

Sizes are not derived from the idle figure alone:

- **typesense 20Gi.** The instantaneous 0.8 GiB understates the real ceiling,
  because typesense is currently indexing nothing (see
  `tk-production-open-edx-course-search-is-indexing-no-da6217`) — so 0.8 GiB is the
  empty-baseline footprint, not a populated index. The bound used instead is
  Meilisearch, which holds the comparable corpus on the same clusters: the largest
  *populated* non-prod course index anywhere is mitxonline QA at 2.35 GiB, against
  14.4 GiB for mitxonline production. Non-prod simply does not carry a
  production-sized course set. 20Gi is ~8x the realistic non-prod ceiling and ~23x
  the current figure.
- **starrocks FE 20Gi.** Not an estimate — `data-ci` has run FE on 20Gi for months
  (3.5% meta, 12.5% log). This applies the size already proven on the same workload
  to QA, which was left on the 100Gi default.
- **clickhouse 100Gi.** The only non-prod volume with a genuine upward trend
  (+1.95 GiB/month, monotonic over 11 weeks). `hot_data_days: 3` caps it
  structurally, and `data-ci` already runs 100Gi at 7.2 GiB. 100Gi is ~11x current
  usage and ~3.8 years of headroom at the observed rate.

Volumes deliberately **not** changed: meilisearch (10Gi, 0.1–2.3 GiB — already
small), clickhouse keeper (20Gi, 0.27 GiB), opik mysql/redis/minio, and the
vantage-kubernetes-agent volumes (10Gi, not deployed from this repo).

## What this reclaims

2,800 GiB of provisioned capacity: 1,920 GiB from typesense, 480 from starrocks FE,
400 from clickhouse.

| | Storage | Provisioned IOPS | Total |
|---|---:|---:|---:|
| With `iopsPerGB: 30` (PR #5869 applied) | $224.00 | $60.00 | **$284.00/mo** |
| With `iopsPerGB: 50` (if #5869 does not land) | $224.00 | $400.00 | **$624.00/mo** |

The two changes overlap heavily and are **not additive** — #5869 already removes most
of the IOPS component by dropping a 100Gi volume to exactly the free 3,000 baseline.
Shrinking below 100Gi therefore saves storage but no further IOPS, except on the
clickhouse volume which is above the baseline at any ratio. Quoting "$450/mo from
#5869 plus $284/mo from this" would double-count.

## Why the config change is not enough

EBS volumes and PVCs can only grow. No operator in play will shrink one:

- **Kubernetes** rejects a PVC shrink outright (`spec.resources.requests.storage:
  field can not be less than previous value`).
- **StatefulSet `volumeClaimTemplates` are immutable.** The API server permits
  updates only to `replicas`, `ordinals`, `template`, `updateStrategy`,
  `persistentVolumeClaimRetentionPolicy` and `minReadySeconds`.
- **The Altinity ClickHouse operator** explicitly resizes PVCs "enlarged only"
  (`pkg/controller/common/storage/storage-reconciler.go`, `reconcilePVC`).

So the smaller size reaches the cluster only when the StatefulSet is recreated, and
the saving is realised only when the old PVC — and with it the old EBS volume — is
deleted. `ebs-gp3-sc` has no `reclaimPolicy` set, so it defaults to `Delete` and the
EBS volume goes when the PVC does; nothing needs deleting on the AWS side.

### The typesense hazard — do not leave this half-done

typesense-operator 0.4.1 makes a config/live mismatch dangerous rather than merely
inert. Its `shouldUpdateStatefulSet` never compares `volumeClaimTemplates`, so the
new size is ignored; but `updateStatefulSet` does `sts.Spec = desired.Spec` followed
by a `client.MergeFrom` patch. Once the CR says 20Gi and the live StatefulSet says
100Gi, the computed merge patch carries a `volumeClaimTemplates` change — which the
API server forbids. That rejects the **entire** patch, so the next time any unrelated
drift triggers an update (image bump, resource change, config change, operator
restart), every subsequent reconcile fails until the two sizes agree again.

The window is narrow and characterisable: changing `storage.size` does not alter the
pod template, so it does not change the operator's hash annotation and does not by
itself trigger an update. Nothing fires until some *other* change does. Complete the
recreate in the same maintenance window and the hazard never arms.

The Altinity operator does not share this problem — its `doUpdateStatefulSet` treats
a failed `Update` as `ErrCRUDRecreate` and deletes/recreates the StatefulSet itself
(`pkg/controller/common/statefulset/statefulset-reconciler.go`). It still will not
shrink the PVC, so step 3 below is required regardless.

## Procedure

Per StatefulSet. Run the ordinals one at a time: every workload here is a quorum
service, and replacing one member's volume lets it re-sync from the survivors
instead of losing the data set. Do not delete all the PVCs of a StatefulSet at once.

### 1. Apply the config

```bash
# typesense — 8 stacks
cd src/ol_infrastructure/applications/edxapp
export EDXAPP_DOCKER_IMAGE_DIGEST=<current digest for the stack>
pulumi up --stack mitx.CI          # then mitx.QA, mitx-staging.{CI,QA},
                                   # mitxonline.{CI,QA}, xpro.{CI,QA}

# starrocks FE — QA only (CI is already 20Gi)
cd src/ol_infrastructure/applications/starrocks && pulumi up --stack lakehouse.QA

# clickhouse hot tier — QA only (CI is already 100Gi)
cd src/ol_infrastructure/applications/clickhouse && pulumi up --stack QA
```

Note: the clickhouse QA and starrocks QA stacks currently carry unrelated unapplied
drift (the ClickHouse backup work from #5817, and external-dns hostname annotations).
Review those previews rather than assuming every step belongs to this change.

### 2. Recreate the StatefulSet so its template picks up the new size

Deleting the StatefulSet leaves the PVCs behind (`whenDeleted` defaults to `Retain`
— see `tk-set-persistentvolumeclaimretentionpolicy-on-non--be059f` for why that
cannot currently be changed). The operator rebuilds the StatefulSet with the new
20Gi template and adopts the existing 100Gi PVCs, which is expected at this stage.

```bash
# typesense, e.g. residential-ci / mitx-openedx
kubectl --context residential-ci -n mitx-openedx delete sts mitx-ts-sts --cascade=foreground
kubectl --context residential-ci -n mitx-openedx get sts mitx-ts-sts \
  -o jsonpath='{.spec.volumeClaimTemplates[*].spec.resources.requests.storage}{"\n"}'
# expect: 20Gi
```

For clickhouse, skip this step — the operator recreates the StatefulSet on its own
when the forbidden update fails. Confirm before continuing:

```bash
kubectl --context data-qa -n clickhouse get sts chi-clickhouse-default-0-0 \
  -o jsonpath='{.spec.volumeClaimTemplates[*].spec.resources.requests.storage}{"\n"}'
```

### 3. Replace the volumes, one ordinal at a time

Deleting the PVC first leaves it `Terminating` on the `kubernetes.io/pvc-protection`
finalizer while its pod still mounts it; deleting the pod releases the finalizer, the
PVC and its EBS volume go, and the StatefulSet controller provisions a fresh PVC at
the new size before rescheduling the pod.

```bash
CTX=residential-ci NS=mitx-openedx STS=mitx-ts-sts
for N in 0 1 2; do
  kubectl --context $CTX -n $NS delete pvc "data-$STS-$N" --wait=false
  kubectl --context $CTX -n $NS delete pod "$STS-$N"
  kubectl --context $CTX -n $NS rollout status sts "$STS" --timeout=10m
  # typesense: confirm the node rejoined the Raft quorum before the next ordinal
  kubectl --context $CTX -n $NS get pods -l app.kubernetes.io/name=typesense
done
```

Volume-claim-template prefixes per workload:

| Workload | PVC name pattern |
|---|---|
| typesense | `data-<sts>-<N>` |
| starrocks FE | `lakehouse-fe-storage-meta-lakehouse-starrocks-fe-<N>`, `lakehouse-fe-storage-log-…` |
| clickhouse hot | `clickhouse-data-chi-clickhouse-default-0-0-0` |

### Data loss per workload

- **typesense** — the index is rebuilt by the course-reindex job; a replica replaced
  one at a time re-syncs from the other two. Non-prod holds ~0.8 GiB of
  empty-baseline data today, so there is effectively nothing to lose.
- **starrocks FE** — the `meta` volume is the catalog. Replacing one ordinal at a
  time is safe (the replaced FE re-replicates from the surviving two); replacing all
  three at once would discard the catalog. Do not batch these.
- **clickhouse hot (QA)** — **single replica, no re-sync path.** Up to
  `hot_data_days: 3` of not-yet-tiered hot data is lost. Confirm the nightly
  `clickhouse-backup` CronJob has a recent successful run, or accept the loss
  explicitly — this is QA.

## Verification

```bash
# 1. No non-prod volume is still oversized
for ctx in applications-ci applications-qa data-ci data-qa \
           operations-ci operations-qa residential-ci residential-qa; do
  echo "### $ctx"
  kubectl --context $ctx get pvc -A \
    -o custom-columns='NS:.metadata.namespace,NAME:.metadata.name,SIZE:.spec.resources.requests.storage,PHASE:.status.phase'
done

# 2. The PVs really shrank (a PVC request without a matching PV is a no-op)
kubectl --context residential-ci get pv \
  -o custom-columns='NAME:.metadata.name,SIZE:.spec.capacity.storage,CLAIM:.spec.claimRef.name,VOL:.spec.csi.volumeHandle'

# 3. The old EBS volumes are gone, not merely detached
aws ec2 describe-volumes --filters Name=status,Values=available \
  --query 'Volumes[?VolumeType==`gp3`].{Id:VolumeId,GiB:Size,Created:CreateTime}' --output table
```

A volume left `available` rather than deleted means its PVC was not actually removed
— check for a stuck `pvc-protection` finalizer.

Re-run the kubelet scrape from the Measurement section afterwards to confirm the
used-bytes figures are unchanged and the percentages simply rose.

## Known interaction: the unschedulable pod-0 problem

Three of these StatefulSets currently have a permanently-`Pending` pod-0
(`mitx-ts-sts-0`, `mitx-staging-ts-sts-0` in residential-qa,
`lakehouse-starrocks-fe-0` in data-qa) — the bound PVC pins the pod to one AZ, the
only `ol.mit.edu/core_node=true` node there is out of CPU, and Karpenter provisions
`core_node=false`. See `tk-three-non-prod-statefulsets-have-a-permanently-u-77261e`
and `tk-qa-starrocks-fe-running-2-3-lakehouse-starrocks--d4ed73`.

This procedure will clear those as a side effect, because a deleted PVC re-binds
wherever there is capacity — which also means **pod-0 is the ordinal most likely to
come back cleanly and the one whose old volume is already detached**. It does not fix
the underlying scheduling trap, so expect it to recur.
