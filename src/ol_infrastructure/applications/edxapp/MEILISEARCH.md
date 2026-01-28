# Meilisearch Setup

## Pulumi Configs

```yaml
  meilisearch:enabled: "true"
  meilisearch:domain: <A public domain that makes sense for the env>
  meilisearch:replica_count: 1 # must be 1
  meilisearch:pv_size: 100Gi
  meilisearch:cpu_request: "250m"
  meilisearch:memory_request: "4Gi"
  meilisearch:memory_limit: "4Gi"

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
      "name": "Default Search API Key",
      "description": "Use it to search from the frontend",
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
It is important that you use "Default Search API Key" and not some other API key that is listed. This one has the right permissions. Copy the `key` and that is the value for `meilisearch_api_key` in the SOPS secrets.

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
