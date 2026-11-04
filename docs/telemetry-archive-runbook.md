# Telemetry archive runbook

The self-hosted Loki (and, from Phase 2, Mimir) archive on the operations EKS
cluster that keeps 13 months of logs and metrics beside Grafana Cloud's 31 days.
Design: `docs/adr/0014-self-hosted-lgtm-long-term-telemetry-retention.md`.
Build order and decisions: `docs/plans/telemetry-archive-implementation-plan.md`.
Code: `src/ol_infrastructure/applications/observability_archive/`.

## What it is, in one paragraph

Every log writer ships twice: once to Grafana Cloud, once to the archive at
`telemetry-archive[-qa].ol.mit.edu` over an internal NLB with basic auth. The
archive is **best effort**: each writer's archive leg has a bounded queue and
drops rather than waits, so a broken archive never delays Cloud. Grafana Cloud
reads the archive over Private Data Source Connect through the `pdc-agent`
Deployment in namespace `observability-archive`, as the datasources
`Loki (archive, 13mo)` (uid `archive-loki`) and later `Mimir (archive, 13mo)`.
Alerts about the archive are tickets (`channel=devops-warnings`), and no alert
rule may read from it.

| | QA | Production |
|---|---|---|
| Cluster | `operations-qa` | `operations-production` |
| Retention | 30 days (canary) | 395 days |
| Loki bucket | `ol-telemetry-archive-loki-qa` | `ol-telemetry-archive-loki-production` |
| Grafana Cloud stack | `mitolqa` | `mitolproduction` |

## Writers and where their drops show

| Writer | Archive leg | Drop signal |
|---|---|---|
| k8s-monitoring collectors (every cluster) | `archive-otlp` destination in `substructure/aws/eks/grafana.py` | `otelcol_exporter_send_failed_log_records_total`, `otelcol_exporter_enqueue_failed_log_records_total` with `exporter=~".*archive.*"` |
| vector-log-proxy (Fastly, Heroku) | `archive.yaml` in its ConfigMap | `vector_buffer_discarded_events_total` (buffer full) and `vector_component_discarded_events_total` (retries exhausted) with `component_id=~"archive_.*"` |
| EC2 Vector (Concourse, Consul, Vault) | `/etc/vector/archive_logs.yaml`, written by cloud-init | journald on the host only |
| EC2 Alloy (Concourse web) | `/etc/alloy/archive.alloy`, written by cloud-init | journald on the host only |

The EC2 files come from `ol_infrastructure/lib/telemetry_archive.py` through
each stack's user-data, so a change there needs a `pulumi up` of the Concourse,
Consul or Vault stack **and** an instance roll. The image ships a discarding
`archive.alloy`, so a host that has not been through cloud-init still starts.

## Erasure: deleting log lines from the archive

Retention is enforced by Loki's compactor, which also serves the delete API.
Deletes are asynchronous: the compactor marks the request, rewrites the
affected chunks without the matching lines on its next retention pass, and
removes the originals after `retention_delete_delay` (2 h). Expect a request to
take **2 to 3 hours** to be fully applied.

### 1. Reach the compactor

```sh
export KUBECONFIG=...   # operations-qa or operations-production
kubectl -n observability-archive port-forward svc/loki-compactor 3100:3100
```

Loki runs with `auth_enabled: false`, so the delete API still wants a tenant
header, and the single tenant is `fake`:

```sh
export LOKI_TENANT=fake
```

### 2. Confirm the lines exist and the selector is tight

Run the stream selector in Grafana against the archive datasource first, or
through the query-frontend:

```sh
kubectl -n observability-archive port-forward svc/loki-query-frontend 3101:3100 &
curl -s -G -H "X-Scope-OrgID: $LOKI_TENANT" \
  'http://localhost:3101/loki/api/v1/query_range' \
  --data-urlencode 'query={service_name="learn-webapp"} |= "user@example.edu"' \
  --data-urlencode 'start=2026-01-01T00:00:00Z' \
  --data-urlencode 'end=2026-01-02T00:00:00Z' \
  --data-urlencode 'limit=20' | jq '.data.result | length'
```

The delete query must be a stream selector, optionally with line filters
(`|=`, `|~`, `!=`, `!~`). Parsers and label filters (`| json`, `| user="x"`) are
not allowed. Keep the time range as narrow as the request permits.

### 3. Submit the delete request

```sh
curl -s -X POST -H "X-Scope-OrgID: $LOKI_TENANT" -G \
  'http://localhost:3100/loki/api/v1/delete' \
  --data-urlencode 'query={service_name="learn-webapp"} |= "user@example.edu"' \
  --data-urlencode 'start=1767225600' \
  --data-urlencode 'end=1767312000'
```

`start` and `end` are Unix seconds. A 204 means the request was recorded.

### 4. Watch it apply

```sh
curl -s -H "X-Scope-OrgID: $LOKI_TENANT" \
  'http://localhost:3100/loki/api/v1/delete' | jq .
```

Status moves `received` → `processed`. A request can be cancelled while it is
still `received`:

```sh
curl -s -X DELETE -H "X-Scope-OrgID: $LOKI_TENANT" \
  "http://localhost:3100/loki/api/v1/delete?request_id=<id>"
```

Loki also filters deleted lines out of query results from the moment the
request is received, so a re-run of step 2 should already return nothing.

### 5. Confirm in S3

Delete requests are stored under `index/delete_requests/` in the Loki bucket,
and the rewritten chunks replace the originals under `fake/` (the tenant
prefix). After the request is `processed` and `retention_delete_delay` has
passed, the compactor's `loki_compactor_deleted_chunks_total` (or the
`loki_compactor_apply_retention_*` family) should have moved and the chunk
count for the affected day should have changed:

```sh
aws s3 ls --recursive s3://ol-telemetry-archive-loki-qa/fake/ | wc -l
```

There is no per-line audit in S3: the proof is the empty query in step 4 plus
the processed request. Record both in the ticket.

### How the lifecycle backstop interacts

The bucket's S3 lifecycle rule expires objects at retention + 7 days, so
anything the compactor misses is removed by S3 a week after it should have
been. The rule is not a substitute for the delete API: it acts on whole
objects by age, never on lines, and it cannot remove a line that sits in a
chunk younger than retention. An erasure request always goes through the
compactor.

## Checks

- **Is the archive down, and is Cloud fine?** Cloud is unaffected by design.
  Confirm with the writers' drop metrics above (they should be rising) and the
  Cloud ingest rate (`grafanacloud_logs_ingest_bytes_total`, flat).
- **Write endpoint**: from a pod in a writer cluster,
  `curl -i https://telemetry-archive-qa.ol.mit.edu/ready` should return 200;
  `curl -i -u <push creds> -X POST .../loki/api/v1/push` with an empty body
  should return 400 from Loki, not 401 from Traefik.
- **Read path**: `kubectl -n observability-archive get deploy pdc-agent`
  shows 2/2; the datasource health check in Grafana Cloud passes.
- **Compactor**: `loki_compactor_apply_retention_last_successful_run_timestamp_seconds`
  is within the last 24 h.

Further sections (ingester restart, compactor stuck, PDC reconnect) land with
Phase 3.
