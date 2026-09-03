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
the OS, so rewriting documents in place inflates `indexSize` permanently. Only a
rebuild through the temp-index-and-swap path (a non-incremental
`./manage.py cms reindex_studio`) reclaims it — `--incremental` writes in place
and does not.

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
meilisearch_master_key: <See below>
meilisearch_api_key: <See below>
```

For the `meilisearch_master_key` I just use a random string from a shell alias I have: `pwgen -s -B -1 64 4`. Anything will work as long as it is at least 16 bytes. More is better, of course.

This is a pain but there isn't a way around it. For `meilisearch_api_key`, you first need to provision the meilisearch instance and have it up and running. Firstly, shell into the running meilisearch-0 pod:
```bash
kubectl exec -it -n mitxonline-openedx meilisearch-0 -- sh
```
Then you need to curl localhost using the `meilisearch_master_key` that you generated earlier, and pull down a short list of default keys that the meilisearch instance provisioned for itself on the first startup. The data is JSON.
```
curl -X GET http://localhost:7700/keys  -H "Authorization: Bearer <YOUR MASTER KEY>" -H "Content-Type: application/json"
<A bunch of JSON>
```
Run that JSON through `jq` to make it readable and you should see an entry in the `results: []` list like this:
```
    {
      "name": "Default Admin API Key",
      "description": "Use it for anything that is not a search operation. Caution! Do not expose it on a public frontend",
      "key": "<Big long hex number>",
      "uid": "b96ad989-66cb-49ca-ae15-475fd7f9a676",
      "actions": [
        "search"
      ],
      "indexes": [
        "*"
      ],
      "expiresAt": null,
      "createdAt": "2026-01-27T14:50:39.011505326Z",
      "updatedAt": "2026-01-27T14:50:39.011505326Z"
    },
```
It is important that you use "Default Admin API Key" and not some other API key that is listed. This one has the right permissions. Copy the `key` and that is the value for `meilisearch_api_key` in the SOPS secrets.

## Reindex Data
After you have the meilisearch instance up and running, you need to reindex all the data from the Open edX platform. This is done by running a management command on the CMS. You can do this by shelling into the CMS pod and running the command like so:
```bash
# Shell into CMS Pod
kubectl exec -it -n mitxonline-openedx <cms-pod-name> -- bash
# Verify that your environment is setup to use meilisearch
grep -iR MEILISEARCH ../config/
../config/cms.env.yml:MEILISEARCH_MASTER_KEY: <The master key you generated earlier>
../config/cms.env.yml:MEILISEARCH_API_KEY: <The API key you pulled from the meilisearch instance>
../config/cms.env.yml:MEILISEARCH_ENABLED: true
../config/cms.env.yml:MEILISEARCH_URL: http://meilisearch:7700
../config/cms.env.yml:MEILISEARCH_PUBLIC_URL: https://<your-meilisearch-domain>
# Run the reindex management command
./manage.py cms reindex_studio --experimental
```
Depending on how much content you have, it could be hours.
