# Meilisearch Setup

## Pulumi Configs

```yaml
  meilisearch:deploy: "true"
  meilisearch:enabled: "true"
  meilisearch:domain: <A public domain that makes sense for the env>
  meilisearch:replica_count: 1 # must be 1
  meilisearch:pv_size: 100Gi
  meilisearch:cpu_request: "250m"
  meilisearch:memory_request: "4Gi"
  meilisearch:memory_limit: "4Gi"
  meilisearch:max_indexing_memory: "2Gi" # optional, see Sizing below
  meilisearch:volume_attributes_class: "" # optional, see Volume throughput below
```

The difference between `deploy` and `enabled`. Deploy means "deploy the helm chart" whereas enabled means "enable meilisearch integration in the Open edX platform". You can deploy the chart but not enable it in Open edX if you want to test things out first.

## Sizing

`max_indexing_memory` sets `MEILI_MAX_INDEXING_MEMORY`, the ceiling on the buffer
Meilisearch fills before flushing a batch to disk. Left unset it defaults to two
thirds of the machine's total memory, which it measures with the `sysinfo` crate
— that reads the host's `/proc/meminfo`, not the cgroup. In a container on a
64GiB node it therefore budgets ~40GiB no matter what `memory_limit` says, never
flushes early, and leaves the kernel to reclaim inside the cgroup instead. Set it
to a fraction of `memory_limit` so the two agree, leaving the remainder for the
process and for page cache over the memory-mapped index. The key is only emitted
when configured, so stacks that omit it are unchanged.

Note this caps the indexing buffer, not total process memory: Meilisearch also
memory-maps its index, and those pages count against the cgroup. Sizing the rest
is a separate question from this knob — check `usedIndexSize`, not `indexSize`,
before reasoning about how much of an index is real data:

```bash
kubectl exec -n <namespace> meilisearch-0 -c meilisearch -- \
  sh -c 'curl -s -H "Authorization: Bearer $MEILI_MASTER_KEY" localhost:7700/stats'
```

`indexSize` is space *allocated* to the index's LMDB file, including free pages;
`usedIndexSize` is what is actually occupied. LMDB never returns freed pages to
the OS, so rewriting documents in place inflates `indexSize` permanently.
`./manage.py cms reindex_studio` does not reclaim it on master or
`release/verawood`: it only populates the course index in place (see Reindex
data below). Two routes remain, and we have not exercised either on our
deployments:

- Meilisearch's own compaction, `POST /indexes/<index>/compact`, which is
  present in the v1.54 source we deploy. It runs as an ordinary Meilisearch task.
  It writes the compacted copy to `data.mdb.copy` beside the live `data.mdb`
  before replacing it, so the PVC needs free space for the index's used size.
- A temp-index-and-swap rebuild. `reindex_studio --libraries-only` does this for
  the library index. For the course index it means calling `api.rebuild_index()`
  (from `openedx.core.djangoapps.content.search`) in a CMS shell, which defaults
  to `incremental=False` and swaps both indexes. It runs in the foreground for
  the length of a full reindex, prints nothing unless given `status_cb=print`,
  needs PVC room for a second copy of the course index, and clears
  `IncrementalIndexCompleted` when it finishes. It holds a 24 hour cache lock
  per index that is only released when the Python process unwinds, so a killed
  shell blocks any other swap rebuild of that index until the lock expires.

## Volume throughput

Whatever cannot be held in page cache is re-read from EBS, so once the index
outgrows the pod's cgroup the volume's throughput becomes the real ceiling on
indexing. `ebs-gp3-sc` provisions gp3 at the AWS default of 125 MB/s, and a
starved instance will sit against that number indefinitely. Over fifteen
consecutive minutes on 2026-09-03, mitxonline production averaged 110 MB/s and
peaked at 122 MB/s — 98% of the ceiling — while peak IOPS reached 1,904/s, only
38% of the 5,000 already provisioned. It read 91.8 GiB in that window against a
12.02 GiB index, so it re-read the whole thing 7.6 times over. Throughput bound
it; IOPS did not. Check both before assuming which one is short.

That ordering flipped once the ceiling moved. With `ebs-gp3-throughput-500`
applied, the same volume peaked at 266 MB/s and 4,626 IOPS over the fourteen
days to 2026-09-15 — 93% of the 5,000 provisioned, against 38% before. Lifting
the throughput ceiling is what let IOPS become the binding constraint, so this
volume now has little IOPS headroom left. It is also why `ebs-gp3-sc` keeps
`iopsPerGB: 50` in production while CI and QA drop to 30.

StorageClass parameters apply at provisioning time only, so raising them does
nothing for a volume that already exists. `volume_attributes_class` names a
`VolumeAttributesClass` instead, which drives `ec2:ModifyVolume` against the live
volume: online, no pod restart, and no rebind — which matters because this PVC
is zone-locked and its pod is pinned to a core node, so anything that forces a
reschedule risks leaving `meilisearch-0` `Pending`.

`ebs-gp3-throughput-500` and its rollback counterpart `ebs-gp3-throughput-125`
are declared for every cluster in `infrastructure/aws/eks/__main__.py` and cost
nothing until a PVC names one. Apply the EKS stack before the stack that
references a class: a PVC naming a class that does not exist yet is held in
`Pending` under `status.modifyVolumeStatus` until it appears. The claim stays
bound and the pod keeps running throughout — it is the modification that waits,
not the volume.

### Rolling back

Do not roll back by reverting the config key. Clearing
`volumeAttributesClassName` means "no class applies"; it does not call
`ec2:ModifyVolume`, so the volume keeps whatever geometry it was last given and
the revert silently leaves it at 500 MB/s. Point the claim at the baseline class
instead, let the modification finish, and only then drop the reference:

```yaml
meilisearch:volume_attributes_class: ebs-gp3-throughput-125
```

Confirm `Throughput` reads `125` on the volume before removing the key, using
the commands below.

EBS allows one modification per volume per six hours, so a mistake here is slow
to walk back. Confirm the result against the volume rather than the PVC:

```bash
kubectl get pvc meilisearch -n <namespace> \
  -o jsonpath='{.status.currentVolumeAttributesClassName}{"\n"}'
aws ec2 describe-volumes --volume-ids <vol-id> \
  --query 'Volumes[0].[Iops,Throughput]' --output text
```

This raises a ceiling; it does not reduce the amount of re-reading. Sizing the
memory limit so the working set fits is the durable fix, and the two are
complementary rather than alternatives.

## SOPS secrets

```yaml
meilisearch_master_key: <a random string of at least 16 bytes>
```

The master key is the only Meilisearch secret stored in SOPS. Generate it with
something like `pwgen -s -B -1 64 1`.

edxapp's API key is derived rather than stored. Meilisearch computes every API
key as HMAC-SHA256(master key, uid), so `meilisearch.py` creates a key with the
fixed uid `EDXAPP_MEILISEARCH_KEY_UID` and the Pulumi program computes the same
value (`edxapp_meilisearch_api_key`) for the `meilisearch_api_key` field of the
`secret-<deployment>/edxapp` Vault secret. edxapp used to carry a hand-copied
"Default Admin API Key", whose uid is random per database, so recreating the
database silently invalidated it (mitx CI, 2026-04-01 to 2026-10-01).

Two workloads create the key, both idempotent (201 on create, 409 when it
already exists): a `Job` that runs once the Helm release is up, and the
`meilisearch-edxapp-key` CronJob, which runs every 15 minutes so a database
recreated later gets the key back. Check it with:

```bash
kubectl exec -n <namespace> meilisearch-0 -c meilisearch -- \
  sh -c 'curl -s -H "Authorization: Bearer $MEILI_MASTER_KEY" localhost:7700/keys/be0444d7-0b85-4dde-a512-0fd3e205902a'
```

The CMS reads the key at pod start, and the Meilisearch Secret is not part of
the CMS config hash, so a changed key reaches Studio on the next CMS rollout.

## Reindex data

Studio search uses two indexes (openedx/openedx-platform#39103, backported to
`release/verawood` in #39122):

- `studio_content` holds `course_block` documents.
- `studio_library_content` holds Libraries V2 documents (`library_block`,
  `library_container`, `collection`).

Library authoring waits synchronously on Meilisearch and indexing cost grows
with index size, so the split keeps library writes off the large course index.
Both names take `MEILISEARCH_INDEX_PREFIX` if one is set.

The indexes are created and configured by a `post_migrate` handler, which our
deploys reach through the `cms-migrate` pre-deploy job, so there is no separate
init command. The handler does nothing when `MEILISEARCH_ENABLED` is false, and
it logs a warning and lets the migration succeed if Meilisearch is unreachable
or the API key is wrong. `reindex_studio` does not apply index settings, and
Meilisearch creates a missing index on the first document write, so populating
before the handler has succeeded leaves an index that exists without its
filterable, searchable and sortable attributes. On a new instance, a deploy has
to run after the edxapp key exists in Meilisearch. Confirm both indexes exist
before populating them:

```bash
kubectl exec -n <namespace> meilisearch-0 -c meilisearch -- \
  sh -c 'curl -s -H "Authorization: Bearer $MEILI_MASTER_KEY" localhost:7700/indexes'
```

Shell into a CMS pod and confirm the environment points at Meilisearch:

```bash
kubectl exec -it -n mitxonline-openedx <cms-pod-name> -- bash
grep -iR MEILISEARCH ../config/
../config/cms.env.yml:MEILISEARCH_MASTER_KEY: <The master key you generated earlier>
../config/cms.env.yml:MEILISEARCH_API_KEY: <HMAC-SHA256 of the edxapp key uid under the master key>
../config/cms.env.yml:MEILISEARCH_ENABLED: true
../config/cms.env.yml:MEILISEARCH_URL: http://meilisearch:7700
../config/cms.env.yml:MEILISEARCH_PUBLIC_URL: https://<your-meilisearch-domain>
```

### New instance

```bash
./manage.py cms reindex_studio
```

This queues a Celery task and returns; follow progress in the CMS worker logs.
Depending on how much content you have, it could be hours. Population is
incremental and tracked per course/library in `IncrementalIndexCompleted`, so an
interrupted run resumes where it stopped when the command is run again. To force
it to start over:

```bash
./manage.py cms shell -c 'IncrementalIndexCompleted.objects.all().delete()'
```

`--experimental`, `--incremental`, `--init` and `--reset` are still accepted but
do nothing except log a warning. Drop them from any runbook or script.

### Upgrading from the single shared index

An instance that was populated before the split has its Libraries V2 documents
in `studio_content`, and Studio now searches `studio_library_content` for them.
That index only holds library items edited since the upgrade until this is run
once.

Deploy the matching authoring MFE first (openedx/frontend-app-authoring#3248 on
master, #3270 on `release/verawood`). An older MFE only searches
`studio_content`, so once this step deletes the library documents from that
index, library search returns nothing.

```bash
./manage.py cms reindex_studio --libraries-only
```

It rebuilds `studio_library_content` through a temp index and swap, then deletes
every non-`course_block` document from `studio_content`. Courses are not
reindexed. Running it again is harmless, with one exception: it clears
`IncrementalIndexCompleted` when it finishes, so do not run it while a plain
`reindex_studio` is in progress or interrupted, or that run starts over and
rewrites every course in place.

Check the result from the Meilisearch pod. `studio_content` should report only
`course_block` and `studio_library_content` the three library types. None of
our stacks set `MEILISEARCH_INDEX_PREFIX`; with one, substitute the prefixed
names:

```bash
kubectl exec -n <namespace> meilisearch-0 -c meilisearch -- sh -c '
  for index in studio_content studio_library_content; do
    curl -s -H "Authorization: Bearer $MEILI_MASTER_KEY" \
      -H "Content-Type: application/json" \
      "localhost:7700/indexes/$index/search" \
      -d "{\"limit\": 0, \"facets\": [\"type\"]}"
    echo
  done'
```

`release/ulmo` does not have the split. There plain `reindex_studio` runs in the
foreground and does a full temp-index rebuild of the single `studio_content`
index, and `--libraries-only` does not exist.
