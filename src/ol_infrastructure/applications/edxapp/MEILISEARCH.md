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
  meilisearch:max_indexing_memory: "4Gi" # optional, see Sizing below
```

The difference between `deploy` and `enabled`. Deploy means "deploy the helm chart" whereas enabled means "enable meilisearch integration in the Open edX platform". You can deploy the chart but not enable it in Open edX if you want to test things out first.

## Sizing

Meilisearch memory-maps its index, so it depends on page cache *inside* the pod's
memory limit to keep that index resident. Size the three memory knobs against the
index rather than against a fixed guess:

- `memory_limit` must exceed `indexSize` + `max_indexing_memory` + roughly 1Gi of
  process heap. Below that the container reclaims its own page cache on every write,
  which stretches single-document index batches from milliseconds to tens of seconds.
- `memory_request` should be close to the expected steady working set, not a token
  value. Kubernetes schedules on the request, so a request far below actual usage lets
  the node be packed to the point where *node-level* reclaim evicts the page cache -
  reintroducing the same thrashing the limit was raised to prevent.
- `max_indexing_memory` sets `MEILI_MAX_INDEXING_MEMORY`. Left unset, Meilisearch
  defaults to a fraction of the memory it believes it has, which inside a container can
  exceed the cgroup limit and lets the indexer evict the page cache it depends on.
  Pin it so the remainder of the limit is available for the resident index.
  The key is only emitted when configured, so stacks that omit it are unchanged.

Read the current index size before choosing values:

```python
c.get_all_stats()  # databaseSize, and per-index indexSize / numberOfDocuments
```

For reference, mitxonline production in August 2026 had a 11.9Gi `studio_content`
index against a 4Gi limit, which produced ~667M working-set refaults and 99.9% direct
reclaim (mitodl/hq#13014).

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
