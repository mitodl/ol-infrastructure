# Telemetry archive runbook

The self-hosted Loki and Mimir archive on the operations EKS cluster that keeps
13 months of logs and metrics beside Grafana Cloud's 31 days.
Design: `docs/adr/0014-self-hosted-lgtm-long-term-telemetry-retention.md`.
Build order and decisions: `docs/plans/telemetry-archive-implementation-plan.md`.
Code: `src/ol_infrastructure/applications/observability_archive/`.

## What it is, in one paragraph

Every log and metric writer ships twice: once to Grafana Cloud, once to the
archive at `telemetry-archive[-qa].ol.mit.edu` over an internal NLB with basic
auth. The archive is **best effort**: each writer's archive leg has a bounded
queue and drops rather than waits, so a broken archive never delays Cloud.
Grafana Cloud reads the archive over Private Data Source Connect through the
`pdc-agent` Deployment in namespace `observability-archive`, as the datasources
`Loki (archive, 13mo)` (uid `archive-loki`) and `Mimir (archive, 13mo)` (uid
`archive-mimir`). Alerts about the archive are tickets
(`channel=devops-warnings`), and no alert rule may read from it.

| | QA | Production |
|---|---|---|
| Cluster | `operations-qa` | `operations-production` |
| Retention | 30 days (canary) | 395 days |
| Loki bucket | `ol-telemetry-archive-loki-qa` | `ol-telemetry-archive-loki-production` |
| Mimir bucket | `ol-telemetry-archive-mimir-qa` | `ol-telemetry-archive-mimir-production` (`blocks/` and `ruler/` prefixes) |
| Grafana Cloud stack | `mitolqa` | `mitolproduction` |

## Writers and where their drops show

| Writer | Archive leg | Drop signal |
|---|---|---|
| k8s-monitoring collectors (every cluster) | `archive-otlp` destination in `substructure/aws/eks/grafana.py` | `otelcol_exporter_send_failed_log_records_total`, `otelcol_exporter_enqueue_failed_log_records_total` with `exporter=~".*archive.*"` |
| vector-log-proxy (Fastly, Heroku) | `archive.yaml` in its ConfigMap | `vector_buffer_discarded_events_total` (buffer full) and `vector_component_discarded_events_total` (retries exhausted) with `component_id=~"archive_.*"` |
| k8s-monitoring collectors, scraped metrics | `archive-metrics` destination in `substructure/aws/eks/grafana.py` | `prometheus_remote_storage_samples_dropped_total`, `prometheus_remote_storage_samples_failed_total` with `url=~".*telemetry-archive.*"` |
| EC2 Vector (Concourse, Consul, Vault) | `/etc/vector/archive_logs.yaml` and `/etc/vector/archive_metrics.yaml`, written by cloud-init | journald on the host only |
| EC2 Alloy (Concourse web) | `/etc/alloy/archive.alloy` (`log_sink` and `metric_sink`), written by cloud-init | journald on the host only |

The EC2 files come from `ol_infrastructure/lib/telemetry_archive.py` through
each stack's user-data, so a change there needs a `pulumi up` of the Concourse,
Consul or Vault stack **and** an instance roll. The image ships a discarding
`archive.alloy`, so a host that has not been through cloud-init still starts.

## Recording rules

Mimir reads raw samples over the whole query range, so every 13-month trend is
pre-aggregated by the ruler. The rules are a `PrometheusRule` named
`telemetry-archive-recording-rules` in `observability-archive`, declared in
`applications/observability_archive/recording_rules.py`; the operations
cluster's `alloy-metrics` syncs it into the ruler every 5 minutes
(`mimir.rules.kubernetes`, namespace prefix `ol-infrastructure`). To see what
the ruler holds:

```sh
kubectl -n observability-archive port-forward svc/mimir-ruler 8080:8080
curl -s http://localhost:8080/prometheus/config/v1/rules
```

Mimir runs with `multitenancy_enabled: false`, so no tenant header is needed.
A rule that fails to evaluate raises `TelemetryArchiveRulerEvaluationFailing`;
the ruler's logs name the expression. Fixing the expression in
`recording_rules.py` and applying the archive stack is the whole fix; the sync
replaces the group on its next pass. Recorded series exist only in the archive.

## Erasure: deleting log lines from the archive

Retention is enforced by Loki's compactor, which also serves the delete API.
Deletes are asynchronous and deliberately slow. A request sits in a
cancellation window first (`delete_request_cancel_period`, Loki's default of
24 h, not overridden here); only once it is older than that does the compactor
rewrite the affected chunks without the matching lines on its next retention
pass, and the originals are removed `retention_delete_delay` (2 h) after that.
Expect **at least 26 hours** from submission until the data is gone from S3.
Query results exclude the lines from the moment the request is received, so
the data is unreadable long before it is physically deleted.

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

Status moves `received` → `processed` once the 24 h cancellation window has
passed and the compactor has run. A request can be cancelled while it is still
`received`:

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
passed (so no sooner than about 26 h after submission), the compactor's `loki_compactor_deleted_chunks_total` (or the
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
  should return 400 from Loki, and the same against `.../api/v1/push` a 4xx
  from Mimir, not 401 from Traefik.
- **Read path**: `kubectl -n observability-archive get deploy pdc-agent`
  shows 2/2; both datasource health checks in Grafana Cloud pass.
- **Compactors**: `loki_compactor_apply_retention_last_successful_run_timestamp_seconds`
  and `cortex_compactor_last_successful_run_timestamp_seconds` are within the
  last 24 h.
- **Series in the archive**: `cortex_ingester_active_series` summed over the
  Mimir ingesters is within a few percent of the Cloud stack's active series.

Further sections (ingester restart, compactor stuck, PDC reconnect) land with
Phase 3.
