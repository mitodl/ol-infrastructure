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
  # meilisearch:course_indexing: "all" # optional; defaults to library_downstream_only, see Course indexing scope below
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

## Course indexing scope

`MEILISEARCH_COURSE_INDEXING` on the CMS controls how much course content Studio
writes to the `studio_content` index. Library content (Libraries V2 blocks,
collections and containers) is indexed in every mode.

| Value | Course blocks indexed |
| --- | --- |
| `all` | Every course XBlock. This is the upstream default. |
| `library_downstream_only` | Only blocks with an `upstream` link to a library. |
| `none` | None. No indexing task is even enqueued. |

**Every deployment with `meilisearch:enabled` set defaults to
`library_downstream_only`.** The default lives in `k8s_configmaps.py` rather
than in each stack, because we run Meilisearch for Libraries V2 and course
search runs on Typesense; indexing course content here only ever cost us
memory. Stacks with Meilisearch disabled get no `MEILISEARCH_COURSE_INDEXING`
key at all.

`meilisearch:course_indexing` overrides the default on a single stack if you
need `all` or `none` for a specific reason. It is validated during the Pulumi
run and anything outside the three values above fails the preview, because the
CMS treats an unrecognised value as `all` — a typo would otherwise silently
restore full course indexing.

The motivating case: as of 2026-08-28 the mitxonline production index held
1,319,937 documents of which 966 were library content, at 11.91 GiB against a
4Gi pod limit. The rest was course XBlocks written by ordinary publishes plus a
batch of course reruns and imports on 2026-08-19/20. See mitodl/hq#13014.

`library_downstream_only` keeps the set the Authoring MFE's course-libraries
Review tab needs — it lists library components used in a course that have
upstream updates ready to sync, and queries the index by `usage_key` to hydrate
them for display. What it gives up, in Studio:

- the course content search modal;
- the block-type breakdown in the course outline info sidebar.

LMS-side courseware search and discovery are unaffected: those run on Typesense
through edx-search, not on Meilisearch.

### Restoring course indexing

The scope is config, not code. To restore it on a stack:

1. Set `meilisearch:course_indexing: all` on that stack and deploy, so the CMS
   emits `MEILISEARCH_COURSE_INDEXING: all`.
2. Confirm the running CMS pods picked it up:
   ```bash
   kubectl exec -n <namespace> <cms-pod-name> -- \
     grep MEILISEARCH_COURSE_INDEXING ../config/cms.env.yml
   ```
3. Rebuild the index so existing course content reappears — ongoing publishes
   only index blocks as they change:
   ```bash
   ./manage.py cms reindex_studio --experimental
   ```
   Use the non-incremental form. It builds into a temp index and swaps, which is
   also the only path that reclaims LMDB free pages (see Sizing above).
4. Size the pod for the result first. A full course index needs far more memory
   and disk than the library-only one; check `usedIndexSize` from `/stats` on a
   comparable environment before committing to `memory_limit` and `pv_size`.

The same steps in reverse apply after *narrowing* the scope on a stack that had
been indexing everything: the setting stops new writes immediately, but the
existing documents only go away on a non-incremental `reindex_studio`, and the
space they occupied is only returned to the filesystem by that same rebuild.
