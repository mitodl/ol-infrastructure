# Implementation plan: self-hosted Loki/Mimir telemetry archive (ADR-0014, Option 2)

Status: plan, ready for review
Date: 2026-11-02
ADR: `docs/adr/0014-self-hosted-lgtm-long-term-telemetry-retention.md`
Repo facts are from `main` at `abcebbd5f`. Chart facts are from the upstream repos on 2026-11-02 and 2026-11-03.

## Scope

How ADR-0014 gets built, in phases that each ship something queryable and each soak in QA
before Production. This plan delivers **Option 2**: logs and metrics are dual-shipped to a
self-hosted Loki and Mimir on the operations EKS cluster and queried from Grafana Cloud over
Private Data Source Connect (PDC). **Tempo is not built.** Traces older than 31 days are not
worth a second tail sampler and a third stateful system; the ADR's trace phase is recorded at
the end as a decision we are not taking, with the evidence that would reopen it.

Everything the ADR fixes (where the archive runs, S3 per backend, IRSA, internal NLB over
peering, PDC for reads, archive-is-best-effort) is taken as given. This plan adds the choices
the ADR left open, names the files each phase touches, and sets the gate each phase has to
pass.

## Decisions

| # | Decision |
|---|---|
| D1 | New Pulumi project `applications/observability_archive/` (`ol-application-observability-archive`), stacks `QA` and `Production`, namespace `observability-archive` on the operations cluster. No `CI` stack: CI clusters run no Alloy. |
| D2 | **QA is a canary, not an archive.** QA runs the same code with 30-day retention and the smallest replica counts, so every phase can soak there for a week. Production is the only 13-month archive. This answers the ADR's open question 2. |
| D3 | **Replication factor 1, single availability zone.** The archive gets its own node group pinned to one AZ. Losing a node loses up to the unflushed window of that ingester (Loki ~1–2 h, Mimir ~2 h) in the archive only. RF2 is worse than RF1 for a ring (quorum of 2 needs both replicas up) and RF3 across zones roughly doubles compute and adds the cross-AZ line in the ADR's cost table. If QA soak shows gaps more often than once a month, the fallback is RF3 with zone-aware replication, no redesign. |
| D4 | Node group `archive-xlarge`: `m8i-flex.xlarge` (4 vCPU / 16 GiB), taint `ol.mit.edu/archive_workload=true:NoSchedule`, label `ol.mit.edu/archive_node: "true"`. QA 2–3 nodes, Production starts at 5 with max 8. Graviton is deferred until the node-group code can pick an arm64 AMI. |
| D5 | **Loki**: official `loki` chart 7.x (Loki 3.6), `deploymentMode: Distributed`, TSDB schema v13, `auth_enabled: false`, compactor retention and delete API on, structured metadata on so OTLP logs land intact. Simple scalable mode is deprecated for Loki 4.0 and monolithic is sized for ~20 GB/day; Production is 119 GB/day. |
| D6 | **Mimir**: `mimir-distributed` chart 6.x in the **classic architecture**. Chart 6.x defaults to Kafka-backed ingest storage with a bundled single-node Kafka, a third stateful system the ADR never sized, so the chart's `classic-architecture.yaml` overrides (`kafka.enabled: false`, `ingest_storage.enabled: false`) are applied first. ADR small-tier replica counts, `replication_factor: 1`, zone-aware replication off, `multitenancy_enabled: false` (writers and the PDC read path bypass the chart's gateway, so nothing would inject `X-Scope-OrgID`), ruler on, alertmanager off, OTLP ingest at `/otlp/v1/metrics`. Blocks and ruler state share one bucket under distinct prefixes (`blocks_storage.storage_prefix: blocks`, `ruler_storage.storage_prefix: ruler`); Mimir refuses to start when they share bucket and prefix. The chart has no monolithic mode, and a hand-written monolithic StatefulSet would be a second way of running Mimir for no saving at 416k series. |
| D7 | **One internal write endpoint**: a dedicated Traefik release in the archive namespace behind an internal NLB (StarRocks's annotations), TLS from the cluster's cert-manager issuer, external-dns record `telemetry-archive-qa.ol.mit.edu` / `telemetry-archive.ol.mit.edu`, basic-auth middleware with one credential per environment. It routes `/loki/api/v1/push` and `/otlp/v1/logs` to the Loki distributor and `/api/v1/push` and `/otlp/v1/metrics` to the Mimir distributor. The NLB security group admits only the peered VPCs' pod CIDRs. |
| D8 | **Reads bypass the NLB.** The PDC agent runs in the archive namespace, so the Grafana Cloud datasources use in-cluster Service URLs (Loki query-frontend, Mimir query-frontend). Nothing on the read path is exposed, and the agent is started with `PermitRemoteOpen` restricted to those two hosts. Two agents per environment, which is the floor for a rolling restart not breaking queries. |
| D9 | **PDC network, PDC token and the two datasources are declared in the archive stack**, with a `pulumiverse_grafana` provider configured from `grafana_cloud/api.<env>.yaml` plus a new `cloud_access_policy_token` key. The archive stack already knows the in-cluster URLs and owns the agent Deployment that consumes the token, so one `pulumi up` builds the whole read path with no cross-stack cycle. Datasource UIDs are fixed: `archive-loki`, `archive-mimir`. |
| D10 | **Failure isolation uses what the k8s-monitoring chart exposes**, verified against chart 4.6.0's destination templates. **Cluster logs reach the archive over the `archive-otlp` destination, not a `loki` destination.** The chart's OTLP destination accepts Loki-sourced logs (it renders an `otelcol.receiver.loki` bridge in front of `otelcol.exporter.otlphttp`), and that exporter's `sendingQueue` is a real non-blocking queue: `blockOnOverflow: false` by default, bounded `queueSize`, plus `retryOnFailure.maxElapsedTime: 1m`. The chart's `loki` destination has no non-blocking option: `loki.write`'s `wal` and `queue_config` blocks are experimental in Alloy, and using them would mean running the production log collectors at the experimental stability level for the archive's benefit. Prometheus destinations get `queueConfig.sampleAgeLimit: 1h`, `queueConfig.maxShards: 10`, `queueConfig.retryOnHttp429: false` (for this destination type the 429 switch lives under `queueConfig`; the top-level key is the Loki destination's) and `writeAheadLog.maxKeepaliveTime: 2h`. Every setting is confirmed in the rendered Alloy config with `helm template` before the first rollout. |
| D11 | **Every feature in `grafana.py` lists its `destinations` explicitly** from Phase 1 on, including the ones that only go to Cloud. Today none does, and the chart's "every destination in the ecosystem" rule would otherwise decide what the archive receives. |
| D12 | Retention: compactor-enforced 395 days (13 months) in Production and 30 days in QA, with an S3 lifecycle rule 7 days behind each as the backstop. Intelligent-Tiering on both buckets. Erasure requests use Loki's compactor delete API, with a runbook. |
| D13 | Archive datasources are **never** valid in alert rules. A unit test in `tests/ol_infrastructure/infrastructure/grafana_alerting/` fails on any rule or dashboard panel referencing the `archive-*` UIDs. |

## Before Phase 0 starts

These do not block writing code, but Production cannot be switched on without them.

- **Retention and privacy sign-off** on 13 months of logs that carry learner identifiers, IP
  addresses and request paths (ADR open question 1). Ask whether any MIT records-retention
  rule sets a floor or a ceiling.
- **Grafana Cloud quote** for longer retention, including any bring-your-own-bucket offer
  (ADR open question 4), so the archive is compared with a real number.
- **Cloud access policy token** per Grafana Cloud stack with `accesspolicies:read/write/delete`
  and `stacks:read`, created by an org admin and added to `src/bridge/secrets/grafana_cloud/api.{qa,production}.yaml` as `cloud_access_policy_token`.

## Phase 0: Foundations (no telemetry yet)

Goal: an empty, reachable archive. Everything that both Loki and Mimir need, built once, so
Phases 1 and 2 are each "add a backend and a destination" and nothing else.

### Work

1. **Node group and namespace** in `src/ol_infrastructure/infrastructure/aws/eks/`:
   - Add `archive-xlarge` to `eks:nodegroups` in `Pulumi.operations.QA.yaml` and
     `Pulumi.operations.Production.yaml` with the taint and label from D4.
   - Add `observability-archive` to `eks:namespaces` in both files.
   - Small change in `__main__.py`'s node-group loop: an optional `availability_zone` key on a
     node group filters `target_vpc["k8s_pod_subnet_ids"]` to that AZ's subnet. Without it the
     group spans three AZs and D3's single-AZ choice is not enforceable. If this turns out to be
     awkward, multi-AZ RF1 is acceptable at roughly $70/month of cross-AZ traffic.
2. **Project skeleton** `src/ol_infrastructure/applications/observability_archive/`:
   `Pulumi.yaml`, `Pulumi.QA.yaml`, `Pulumi.Production.yaml`, `__main__.py`, and modules
   `storage.py` (buckets + IRSA), `ingress.py` (Traefik + NLB + DNS + auth), `pdc.py`,
   `loki.py`, `mimir.py`. Stack references: `projects.NETWORKING` for VPC CIDRs,
   `projects.EKS` (`operations.<env>`) for the kubeconfig, cluster identities and Vault auth
   endpoint, the same way `infrastructure/vector_log_proxy/__main__.py` does.
3. **Storage**: `ol-telemetry-archive-loki-<env>` and `ol-telemetry-archive-mimir-<env>` via
   `components/aws/s3.py`'s `OLBucket`, Intelligent-Tiering, lifecycle expiry at retention + 7
   days (on the Mimir bucket filtered to the `blocks/` prefix, so ruler state never expires; see
   D6), versioning off. One IRSA role per backend through `OLEKSAuthBinding`
   (`create_irsa_service_account=False`; the charts create the ServiceAccounts and we pass the
   role ARN as an annotation). Policies linted with `lint_iam_policy`.
4. **Ingress** (D7): Traefik Helm release (`TRAEFIK_CHART_VERSION`) with its Service annotated
   `aws-load-balancer-scheme: internal`, `nlb-target-type: ip`, an explicit security group,
   and `external-dns.kubernetes.io/hostname`. Security group ingress on 443 from the
   `k8s_pod_subnet_cidrs` of the applications, data and residential VPCs and the operations
   VPC CIDR, all read from the network stack outputs. Certificate from the cluster issuer.
   Basic-auth middleware fed by a Kubernetes Secret that the Vault Secrets Operator syncs from
   a new KV mount `secret-observability-archive`, which Pulumi seeds from
   `src/bridge/secrets/observability_archive/archive.<env>.yaml` (new SOPS file: push
   username and password). Routes are added in Phases 1 and 2; Phase 0 ships `/ready`.
5. **PDC** (D8, D9): `grafana.cloud.PrivateDataSourceConnectNetwork` and
   `PrivateDataSourceConnectNetworkToken` (region `prod-us-central-0`, matching the stacks),
   token into a Kubernetes Secret, `grafana/pdc-agent` Deployment with 2 replicas pinned to the
   archive node group, flags `-connections=2` and `-ssh-flag='-o PermitRemoteOpen=<loki-qf>:3100 <mimir-qf>:8080'`.
   Pin the image tag in `bridge/lib/versions.py`. Confirm `pulumiverse_grafana.oss.DataSource`
   in the pinned provider version accepts `private_data_source_connect_network_id`; the
   Terraform resource does.
6. **Versions**: `LOKI_CHART_VERSION`, `MIMIR_DISTRIBUTED_CHART_VERSION`, `PDC_AGENT_VERSION`
   in `src/bridge/lib/versions.py` with Renovate comments.
7. **Labels**: every workload gets `cluster_addon_labels(... service=<new Services.telemetry_archive>, alert_tier=AlertTier.ticket)` so the archive's own alerts route as tickets and never page (ADR step 6). Add the enum member to `lib/ol_types.py`.

### Gate (QA, then Production)

- From a pod on `applications-<env>`, `curl https://telemetry-archive-<env>.ol.mit.edu/ready`
  returns 200 and `dig` resolves to private IPs only.
- `grafanacloud_grafana_pdc_connected_agents` for the new network reads 2 in the matching
  Cloud stack.
- No archive bytes in the source VPCs' NAT `BytesOutToDestination` (nothing is shipped yet;
  this establishes the baseline for Phase 1's check).

## Phase 1: Loki (logs)

Goal: every log stream that reaches Grafana Cloud also reaches the archive, after the same
processing, and `Loki (archive, 13mo)` works in Explore and Logs Drilldown.

### Work

1. **Loki release** in `loki.py` (D5): distributed mode, RF1, S3 via IRSA, TSDB v13 schema
   starting on the deploy date, `limits_config.retention_period` 395d/30d,
   `compactor.retention_enabled`, `delete_request_store: s3`, structured metadata on, the
   chart's memcached caches at their smallest size. Replicas from the ADR's table less the RF:
   ingester 2 (3 in Production once volume is confirmed), distributor 2, querier 2, query-frontend
   1, query-scheduler 1, index-gateway 1, compactor 1. Everything scheduled onto the archive
   node group with the D4 toleration. Chart self-monitoring off; `monitoring.serviceMonitor.enabled`
   on (the chart has no root-level `serviceMonitor` key) so the existing
   `prometheusOperatorObjects` feature scrapes it into Cloud.
2. **Ingress routes**: `/loki/api/v1/push` and `/otlp/v1/logs` to the Loki distributor
   Service, behind the basic-auth middleware.
3. **k8s-monitoring destinations** in `src/ol_infrastructure/substructure/aws/eks/grafana.py`:
   - `archive-otlp` (`otlp`, `protocol: http`, `url: https://telemetry-archive-<env>.ol.mit.edu/otlp`):
     basic auth from a new key pair in `alloy/grafana.<env>.yaml`, `logs.enabled: true`,
     `metrics.enabled: false` until Phase 2, `traces.enabled: false` permanently, D10's queue
     and retry settings. No `tailSampling` block, so no second sampler is created. OTLP logs
     default to `<url>/v1/logs`, which is exactly Loki's native OTLP path. No `loki`-type archive
     destination exists (D10).
   - Explicit `destinations` on every feature (D11): `podLogsViaLoki` and `clusterEvents` to
     `[grafana-cloud-logs, archive-otlp]`; `applicationObservability` to
     `[gc-otlp-endpoint, archive-otlp]`; `clusterMetrics`, `costMetrics`,
     `annotationAutodiscovery`, `prometheusOperatorObjects`, `integrations` to
     `[grafana-cloud-metrics]`.
   - Run `helm template` against the pinned chart and read the rendered `loki.write`,
     `otelcol.receiver.loki` and `otelcol.exporter.otlphttp` blocks: one Cloud and one archive
     writer per feature, `block_on_overflow = false` and the retry values present, no feature
     fanned out to a destination it was not given, and nothing the collectors' default
     `generally-available` stability level would reject.
   - `alloy-logs` and `alloy-singleton` memory requests go up for the second exporter queue;
     measure in QA.
4. **vector-log-proxy** (`src/ol_infrastructure/infrastructure/vector_log_proxy/__main__.py`):
   two new `loki` sinks, `archive_fastly_logs` and `archive_heroku_logs`, with the same `inputs`
   and `labels` as the Cloud sinks, `endpoint` set to the archive hostname, and bounded
   buffering: `buffer.type: memory`, `buffer.max_events`, `buffer.when_full: drop_newest`,
   `request.retry_attempts: 3`. Credentials via the existing Vault KV + VSO path and two new
   environment variables on the Deployment.
5. **EC2 Vector**: the Concourse, Consul and Vault images do not install
   `components/vector/templates/global_log_sink.yaml` (`use_global_log_sink` is false
   everywhere); each ships its own sink template. A second `loki` sink with the same `inputs`,
   `labels` and a bounded buffer goes into
   `images/concourse/templates/vector/concourse_logs.yaml`,
   `images/consul/templates/vector/consul_logs.yaml` and
   `images/vault/templates/vector/vault_logs.yaml`, reading `ARCHIVE_LOKI_ENDPOINT`,
   `ARCHIVE_LOKI_USER` and `ARCHIVE_LOKI_PASSWORD`. Those variables live in
   `/etc/default/vector`, which is not an image file: Pulumi writes it through cloud-init
   user-data in `applications/concourse/__main__.py` (worker and web),
   `infrastructure/consul/__main__.py` and `infrastructure/vault/__main__.py`, so each of those
   stacks gets the three values and a `pulumi up` that rolls the instances. `steps.py`'s
   `vector validate` environment gets placeholders. Both the AMI rebuild and the stack updates
   have to land before archive logs flow, and the image build's `vector validate` confirms
   Vector still starts with the variables unset.
6. **EC2 Alloy** (`src/bilder/components/alloy/files/config.alloy`, Concourse only): a second
   exporter `otelcol.exporter.otlphttp "archive"` added beside `otelcol.exporter.loki` in the
   log pipeline's `output.logs`, pointed at `https://telemetry-archive-<env>.ol.mit.edu/otlp`,
   with the default non-blocking `sending_queue` (`block_on_overflow = false`) and
   `retry_on_failure { max_elapsed_time = "1m" }`. A second `loki.write` would not do: its
   `wal` and `queue_config` are experimental, and without them a stalled archive writer
   back-pressures the shared `otelcol.exporter.loki` and delays Cloud delivery. The three
   archive variables go into `/etc/default/alloy`, which `applications/concourse/__main__.py`
   also renders through user-data.
7. **Datasource** `Loki (archive, 13mo)`, uid `archive-loki`, URL
   `http://<loki-query-frontend>.observability-archive.svc.cluster.local:3100`, PDC network from
   Phase 0, in the archive stack.
8. **Alerts** (ticket tier, in `infrastructure/grafana_alerting/metric_rules/`): archive
   writer drop rate above zero for 30 minutes (Alloy `otelcol_exporter_send_failed_log_records_total`
   and `otelcol_exporter_enqueue_failed_log_records_total` on the archive exporter, Vector
   `vector_component_discarded_events_total` on the `archive_*` sinks), compactor retention not run in 24 hours, PDC connected agents below 2 for 15
   minutes. Confirm each metric name against what actually arrives in Cloud before the rule
   lands.
9. **D13 test**: `test_no_archive_datasource_in_alert_rules.py` walks the rule and dashboard
   modules and fails on `archive-loki` or `archive-mimir`.
10. **Erasure runbook** `docs/telemetry-archive-runbook.md`: how to issue and verify a
    compactor delete request through a port-forward, how to confirm it in S3, and how the
    lifecycle backstop interacts with it.

### Gate (QA for one week, then Production)

- **Failure isolation**: scale the Loki distributors to zero for one hour. Cloud
  `grafanacloud_logs_ingest_bytes_total` rate, `alloy-logs` memory, vector-log-proxy memory,
  pod-log lag (`loki_source_file_read_lines_total` rate), and the Concourse hosts' Vector and
  Alloy memory and Cloud log rate all stay flat. Drop counters rise and then stop when
  distributors return.
- **No double send**: `grafanacloud_*_bytes_received` in Cloud is unchanged across the
  rollout.
- **Parity**: for three services, `count_over_time({service_name="x"}[1h])` agrees between the
  two datasources within the archive's drop count. Redacted Keycloak lines are redacted in the
  archive too.
- **Transport**: archive traffic is absent from source-VPC NAT `BytesOutToDestination` and
  present on the NLB's `ProcessedBytes`.
- **Soak**: no ingester restart loop, compactor running daily, S3 growth per day within the
  ADR's 12–24 GB/day compressed estimate.

### Phase 1 as built (2026-11-04)

Branch `md/202611_telemetry_archive_phase0`, on top of Phase 0. Where the build departs from
the text above:

- **One source of truth for writers.** `lib/telemetry_archive.py` holds the archive hostnames,
  push paths, datasource UIDs, the push-credential read, and the shape of a bounded Vector sink.
  The archive stack takes its domain from it too (the `observability_archive:domain` config key
  is gone), and every writer reads the push credential from
  `observability_archive/archive.<env>.yaml` instead of a copy in `alloy/grafana.<env>.yaml`.
- **EC2 Vector needs no AMI rebuild.** Instead of a second sink in each image's log template,
  cloud-init writes `/etc/vector/archive_logs.yaml` beside the image's templates; Vector merges
  every file in its config directory. The sink's inputs and labels are a table in the lib module
  that `tests/ol_infrastructure/lib/test_telemetry_archive.py` checks against the image
  templates, which is what stands in for `vector validate` on a file the image never sees. CI
  hosts get no file and ship to Cloud alone.
- **EC2 Alloy imports a module.** `config.alloy` fans logs out to `archive.log_sink.default.input`
  from `import.file "/etc/alloy/archive.alloy"`. The image ships that file as a discarding
  `otelcol.exporter.loki { forward_to = [] }`; cloud-init overwrites it on QA and Production
  hosts with an `otelcol.exporter.otlphttp` whose endpoint and credential come from
  `/etc/default/alloy`. An unset endpoint would fail Alloy's initial load outright
  (`otlphttp` Validate: "at least one endpoint must be specified"), so CI cannot simply leave
  the variables empty, and the module is how the image stays environment-agnostic.
- **vector-log-proxy** also gains Vector's `internal_metrics`, a `prometheus_exporter` on
  `:9598` and a PodMonitor, which is what the drop-rate alert reads.
- **Alerts** live in `metric_rules/telemetry_archive.py`: Alloy and Vector archive drop rates,
  compactor retention older than a day, and `pdc-agent` available replicas below 2 (from
  kube-state-metrics, which already reaches Cloud for the namespace, rather than a
  `grafanacloud-usage` series the rules' datasource cannot see). EC2 Vector and Alloy drops are
  not covered: neither host scrape carries the counters.
- **Verified by `helm template`** against the pinned charts: the k8s-monitoring render shows one
  Cloud and one archive writer per log feature, `archive_otlp` fed only logs, metrics features
  to Cloud only, and `retry_on_failure`/`sending_queue` as specified; `block_on_overflow` is
  absent because the chart renders it only when true and the default is false. The Loki render
  shows one ingester StatefulSet (zone-aware replication off), the IRSA annotation, ECR-cached
  images, and the retention, schema and compactor settings in the generated config.
- **Not yet done**: the QA soak and its gates, the Production rollout, and the first instance
  roll of the Concourse, Consul and Vault hosts after their stacks are applied.

## Phase 2: Mimir (metrics)

Goal: every series Cloud receives also reaches the archive, OTLP metrics included, and
13-month trends are answerable from recording rules in `Mimir (archive, 13mo)`.

### Work

1. **Mimir release** in `mimir.py` (D6): the chart's `classic-architecture.yaml` overrides
   first (`kafka.enabled: false`, `ingest_storage.enabled: false`), then ADR replica counts
   (distributor 2, ingester 3, querier 2, query-frontend 1, store-gateway 1 in QA / 3 in
   Production, compactor 1), RF1, zone-aware replication off, `multitenancy_enabled: false`,
   `compactor_blocks_retention_period` 395d/30d, blocks storage on S3 via IRSA under
   `blocks_storage.storage_prefix: blocks`, ruler on with `ruler_storage.storage_prefix: ruler`
   in the same bucket, alertmanager and overrides exporter off, `metaMonitoring.serviceMonitor`
   on. `helm template` must show no Kafka StatefulSet and no `ingest_storage` block. Limits
   raised to Production's 416k active series with headroom (`max_global_series_per_user`,
   `ingestion_rate`).
2. **Ingress routes**: `/api/v1/push` and `/otlp/v1/metrics` to the Mimir distributor.
3. **k8s-monitoring destinations** in `grafana.py`:
   - `archive-metrics` (`prometheus`): `url: https://.../api/v1/push`, D10's queue and WAL
     settings. Add it to the `destinations` of `clusterMetrics`, `costMetrics`,
     `annotationAutodiscovery`, `prometheusOperatorObjects` and `integrations`. `opencost`'s
     `metricsSource` stays `grafana-cloud-metrics`.
   - `archive-otlp`: set `metrics.enabled: true`. Lift the `processors.transform.metrics`
     statement (`aggregate_on_attributes("sum") where IsMatch(name, "^http[.]server[.]")`) and
     the `filters` block out of `gc-otlp-endpoint` into module-level constants and reference
     them from both OTLP destinations, so the unmatched-path collapse yields one valid series in
     the archive as well.
   - `helm template` again: each `prometheus.remote_write` component is its own WAL on the
     `alloy-metrics` StatefulSet, so raise its memory request and confirm in QA.
4. **EC2 Vector** and **EC2 Alloy**: `global_metric_sink.yaml` is as unused as its log
   counterpart (`use_global_metric_sink` is false everywhere). Host metrics on all three images
   ship from `components/vector/templates/host_metrics.yaml` and Concourse's application
   metrics from `images/concourse/templates/vector/concourse_metrics.yaml`, so each of those
   gets a second `prometheus_remote_write` sink. EC2 Alloy gets a second
   `prometheus.remote_write "archive"` with `queue_config { sample_age_limit = "1h" }` (both
   generally available). Same variables and rollout as Phase 1 step 5: AMI rebuild plus
   user-data updates in the three stacks.
5. **Datasource** `Mimir (archive, 13mo)`, uid `archive-mimir`, type `prometheus` with
   `prometheusType: Mimir`, URL `http://<mimir-query-frontend>.observability-archive.svc.cluster.local:8080/prometheus`.
6. **Recording rules for long ranges.** Mimir reads raw samples over the whole range, so each
   trend we care about is pre-aggregated: per-service RED from the OTLP `http_server_duration`
   histograms (the replacement for Cloud's span metrics), CPU and memory by namespace, pod and
   series counts by cluster, ingest bytes by signal. Declared as `PrometheusRule` objects in the
   archive namespace and synced to the ruler by the operations cluster's `alloy-metrics` through
   the `archive-metrics` destination's `rules.enabled`, `rules.address` and `rules.namespaces`
   settings, so rules are code in this repo and not state inside Mimir.
7. **Alerts** (ticket): remote-write dropped samples (`prometheus_remote_storage_samples_dropped_total`
   on the archive component), Mimir compactor last successful run older than 24 hours, ruler
   evaluation failures.

### Gate (QA for one week, then Production)

- **Failure isolation**: Mimir distributors to zero for one hour. Cloud
  `grafanacloud_instance_samples_per_second`, `alloy-metrics` memory and scrape health stay
  flat; `prometheus_remote_storage_samples_dropped_total` rises on the archive component only,
  WAL on disk stays under the 2 h keepalive.
- **No double send** and **transport** checks as in Phase 1.
- **Parity**: `sum(rate(container_cpu_usage_seconds_total[5m])) by (namespace)` over the last
  hour agrees between datasources within 1 %. Active series in the archive
  (`cortex_ingester_memory_series`) matches Cloud's 416k within 5 %.
- **QA series**: QA's 225k series on the canary's three ingesters stays under the per-ingester
  limit with 50 % headroom, or the canary's limits are raised, not its replica count.

## Phase 3: Hardening and handover

Goal: the archive is something the team can neglect safely.

1. **Dashboard** `Telemetry archive health` in `grafana_alerting/dashboards/`: ingest and drop
   rates per writer, ingester memory, compactor age, S3 bytes per bucket, PDC agents and p90
   query latency from `grafanacloud-usage`.
2. **Cost review after the first Production month**: S3 request charges, NLB processed bytes,
   archive-VPC NAT bytes (PDC query egress), node-group hours. Record actuals against the ADR's
   table in the ADR itself.
3. **Right-size** the node group and replica counts from a month of Production data. This is
   where the ADR's "re-derive the best-effort column" happens.
4. **Upgrade cadence**: a quarterly recurring task on the team board covering the Loki, Mimir,
   PDC agent and Traefik versions, with Renovate already proposing the bumps.
5. **ADR-0014**: status to Accepted, decision text updated to Option 2 as delivered, open
   questions 1–4 answered, and the "traces" decision below recorded.
6. **Runbook**: extend `docs/telemetry-archive-runbook.md` with ingester restart, compactor
   stuck, PDC reconnect, and "the archive is down, is Cloud fine" checks.

## Not planned

**Tempo and a second tail sampler (ADR phase 3).** Not built. Traces are kept 31 days in
Cloud only. The ADR asked who decides and on what evidence; the answer recorded here is: the
infrastructure team reopens this only if, over two consecutive quarters, more than one
investigation per quarter needed a trace older than 31 days and could not be answered from
archived logs plus the RED recording rules in Phase 2. Building it later costs what the ADR
estimated (1–1.5 weeks) with no penalty for waiting, because the archive OTLP destination
already exists and only needs `traces.enabled` and a sampler block.

**Option 5 (trimming Cloud ingest to fund the archive).** A later decision with its own ADR
amendment, after at least one semester of archive history exists.

**Backfill of the 31 days in Cloud.** Not planned, as in the ADR.

## Effort and order

| Phase | Build | Soak | Notes |
|---|---|---|---|
| 0 Foundations | ~1 week | none | Blocked on the cloud access policy token for PDC |
| 1 Loki | ~1.5 weeks | 1 week QA | Largest phase; EC2 AMI rebuilds and user-data stack updates can trail the EKS rollout |
| 2 Mimir | ~1 week | 1 week QA | Recording rules can land during the soak |
| 3 Hardening | ~3 days | first Production month | Cost review needs a full billing cycle |

Roughly six to seven calendar weeks to a Production archive holding logs and metrics, with
Production switched on only after the sign-offs listed under "Before Phase 0 starts".

## Risks carried from the ADR, and where this plan catches them

| Risk | Caught by |
|---|---|
| Archive back-pressure stalls the Cloud stream | D10 settings, `helm template` review, distributor-to-zero gate in Phases 1 and 2 |
| Chart fans a feature to the wrong destination and Cloud ingest doubles | D11 explicit lists, `helm template` review, `grafanacloud_*_bytes_received` gate |
| Collectors run out of memory with a second exporter queue or remote-write WAL | QA soak with memory requests raised before Production |
| Archive traffic takes the NAT path | Hostname resolves to private IPs only; NAT `BytesOutToDestination` gate |
| RF1 loses data on a node failure | Accepted for a best-effort archive (D3); gap frequency measured in QA soak; RF3 fallback named |
| Someone alerts on the archive | D13 test |
| Nobody upgrades it | Phase 3 recurring task and Renovate pins |
