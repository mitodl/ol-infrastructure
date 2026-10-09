# 0014. Dual-Ship Telemetry to a Self-Hosted Loki/Mimir/Tempo Archive for Long-Term Retention

**Status:** Proposed
**Date:** 2026-10-08
**Deciders:** Platform Engineering team
**Technical Story:** TBD

## Context

### Current Situation

All logs, metrics and traces go to Grafana Cloud, and only there. Grafana Cloud has
three stacks (`mitolproduction`, `mitolqa`, `mitolci`), all on the `advanced` plan.
Each keeps data for 744h, which is 31 days. Longer retention costs more than we can
pay.

There is one write path for each kind of source:

| Source | Shipper | Defined in |
|--------|---------|------------|
| EKS pod logs, cluster events, cluster/app metrics, OTLP traces/metrics/logs | Alloy, via the `k8s-monitoring` Helm chart (4 collectors per cluster plus a tail sampler) | `src/ol_infrastructure/substructure/aws/eks/grafana.py` |
| Heroku log drains and Fastly real-time logs | `vector-log-proxy` (Vector on EKS) | `src/ol_infrastructure/infrastructure/vector_log_proxy/` |
| EC2 hosts (Concourse, Consul, Vault) | Vector global log and metric sinks, plus Alloy for OTLP on Concourse only | `src/bilder/components/vector/templates/global_*_sink.yaml`, `src/bilder/components/alloy/files/config.alloy` |

CI clusters run no Alloy (`setup_grafana` returns early, and `lib/otel.py` follows the
same rule). Their only telemetry is the small amount that EC2 Vector ships.

Ingest volume, averaged over the 7 days to 2026-10-08 (`grafanacloud-usage`
datasource):

| Stack | Logs | Traces (after tail sampling) | Active series | Samples/s |
|-------|------|------------------------------|---------------|-----------|
| production | 1.38 MB/s ≈ **119 GB/day** | 0.59 MB/s ≈ **51 GB/day** | **416k** | 8.1k |
| qa | 66 KB/s ≈ 5.7 GB/day | 14 KB/s ≈ 1.2 GB/day | 225k | 5.2k |
| ci | 9.5 KB/s ≈ 0.8 GB/day | negligible | 0.9k | 33 |

### Problem Statement

A month of history is not enough for:

- Investigating incidents and security events that come to light more than 31 days
  after they happened, and answering audit or legal requests about them.
- Planning capacity and cost from year-over-year and semester-over-semester trends.
  Our traffic is seasonal (course runs, term starts), so a 31-day window never holds
  the previous comparable peak.
- Checking a regression against the same period last term.

### Constraints

- The Grafana Cloud bill cannot rise much. Buying longer retention there is the
  baseline we are trying to avoid, not an option we can take.
- Grafana Cloud stays the system people work in. Dashboards, alerting
  (`infrastructure/grafana_alerting`), Rootly routing, the Kubernetes and Application
  Observability apps, and Fleet Management all keep running against Cloud as they do
  today. Nothing that pages anyone may come to depend on the new system.
- Every self-hosted component is an operational burden for a small team. Whatever we
  build has to be cheap to run and safe to neglect.
- An archive outage must never slow or drop the Grafana Cloud stream. The archive is
  best-effort. Cloud is not.

### Assumptions

- **Retention targets** (open question, see Notes): 13 months for logs and metrics,
  so that a full academic year can be compared with the one before, and 90 days for
  traces.
- The archive matters for production. Whether QA is archived as well (see Notes) is
  undecided, and CI is not archived.
- The archive is queried rarely: a few investigations a week and nothing on a
  schedule. Query latency of tens of seconds over long ranges is acceptable.

### Options Considered

#### Option 1: Full mirror. Dual-write every signal to a self-hosted LGTM backend and query it from Grafana Cloud. *(proposed design)*

Each shipper gets a second destination that carries the same data, after the same
processing, to self-hosted Loki, Mimir and Tempo. These run on EKS with S3 for
storage. Grafana Cloud reaches them as extra datasources through **Private Data
Source Connect (PDC)**, an agent in our cluster that holds an outbound tunnel open to
Grafana Cloud. Nothing has to be exposed to the internet.

```
                         ┌──────────────── Grafana Cloud (31d) ───────────────┐
 apps ─► Alloy / Vector ─┤ Loki · Mimir · Tempo   ◄── dashboards, alerts, apps │
           │             └────────────────────────────────▲───────────────────┘
           │ (2nd destination, same processed stream)     │ PDC tunnel (outbound)
           ▼                                              │
   ┌──────────── self-hosted archive (operations EKS) ────┴───┐
   │ loki-archive · mimir-archive · tempo-archive   pdc-agent │
   └───────────────────────────┬──────────────────────────────┘
                               ▼
                 S3 (13mo logs/metrics, 90d traces)
```

- Pros:
  - The archive receives the same shipper-processed streams and uses the same query
    languages. Most queries can be reused by switching datasource, but Cloud-derived
    data, independently sampled traces and translated OTLP metrics may differ.
  - Every query language and tool people already know (LogQL, PromQL, TraceQL) works
    on the archive.
  - It needs no inbound exposure, because PDC dials out from our side.
  - The archive comes up empty, with nothing to migrate. It starts filling from the
    day it is turned on.
- Cons:
  - We take on three stateful distributed systems, plus their upgrades, sizing and
    on-call surface.
  - Collectors carry more load: a second WAL and queue for each destination and, for
    traces, a second tail sampler (see Decision).
  - Data that Grafana Cloud *derives* is absent from the archive. That covers Tempo's
    metrics-generator series (span metrics, service graphs), Adaptive Metrics
    aggregations and Cloud-side recording rules. We would have to run those ourselves
    if we want them.
  - Grafana Cloud does not handle any cross-datasource stitching for us. Trace-to-logs
    links, exemplars and correlations have to be configured a second time for the
    archive datasources.

#### Option 2: Tiered mirror. Archive logs and metrics only, and keep traces in Cloud.

The same as Option 1, minus Tempo. Traces keep their 31 days in Cloud and nowhere
else.

- Pros:
  - About a third less to run. Tempo and the second tail sampler are the most fragile
    parts of Option 1 (see the long sizing history in `grafana.py`).
  - Most of the long-term value is in logs (audit, forensics) and metrics (trends).
    Traces older than a month are rarely opened. The RED metrics derived from them are
    what people want long-term, and those can be kept as recording rules in
    mimir-archive.
- Cons:
  - We cannot open individual old traces.
  - Adding traces later means the same work as Option 1's trace phase, with no
    savings.

#### Option 3: Cold archive. Dual-write raw telemetry to S3 with no query engine.

The second destination writes compressed, partitioned objects straight to S3. Alloy's
`otelcol.exporter.awss3` and Vector's `aws_s3` sink both do this. The Alloy component is still experimental. Nothing runs all the
time. To answer a question we either query with Athena (or with StarRocks, through a
Glue table over the prefix), or "rehydrate" a time range into a temporary Loki.

- Pros:
  - The cheapest by a wide margin: S3 storage plus a little shipper CPU, with no
    service to keep up.
  - It fits tooling the data platform team already runs (Glue, Athena, StarRocks).
  - Retention costs next to nothing. Glacier tiers make multi-year retention cheap.
- Cons:
  - It cannot be queried from Grafana at all. Every investigation becomes a SQL or
    rehydration exercise, which in practice means only one or two people ever use it.
  - Metrics stored as raw OTLP or remote-write objects are close to unqueryable in
    SQL at any useful scale.
  - Labels, query semantics and every link back to Grafana Cloud are lost.

#### Option 4: Self-hosted as the long-term store, with Cloud trimmed to fund it.

Build Option 1, then use the archive to cut Cloud spend. Drop or aggregate
high-volume, low-value streams before the *Cloud* destination only, for example
debug-level logs, Traefik and APISIX access logs beyond what alerting needs, and
high-cardinality series that only feed long-range analysis. The archive keeps
everything.

- Pros:
  - The archive could pay for itself or better. Production logs are 119 GB/day, and
    even a modest cut there is real money every month.
- Cons:
  - Cloud and the archive then disagree about what exists. "It isn't in Cloud, check
    the archive" becomes a habit people have to learn, and the archive becomes
    something we depend on rather than a convenience.
  - This adds the per-destination processing that Option 1 deliberately avoids.

This option is a later step after Option 1, not an alternative to it, and is listed
so the door is left open.

#### Option 5: Buy longer retention from Grafana Cloud.

- Pros:
  - It needs no new infrastructure at all.
- Cons:
  - It is ruled out on cost by the constraints above. We should still get a current
    quote, including any bring-your-own-bucket or export offering, so that Options
    1–3 are compared with a real number rather than an assumed one.

## Decision

- **Chosen Option:** Option 1, the full mirror, delivered signal by signal: **logs →
  metrics → traces**. After the metrics phase we stop and decide whether traces are
  worth it. If they are not, we end at Option 2.
- **Rationale:** Being able to query the archive from Grafana Cloud with the same
  labels and languages is what makes it actually get used. Option 3 is cheap only
  until the first investigation. Phasing puts the most valuable and most
  straightforward signal (logs) first, and puts off the costliest piece (Tempo plus a
  second tail sampler) until there is evidence it is needed.

### Key Implementation Details

**1. Split each stream once, after all processing.** The second destination has to
receive the stream *after* relabelling, PII redaction
(`_keycloak_olapps_idp_login_redact_alloy_config`), span filtering and span status
rewrites. That keeps the archive identical to Cloud, and it means the archive never
holds data that Cloud had redacted. Per shipper:

- **k8s-monitoring (`grafana.py`)**: add `archive-logs` (`loki`), `archive-metrics`
  (`prometheus`) and `archive-otlp` (`otlp`, traces only) to `destinations`. Each
  feature's `destinations` list should be set *explicitly*. Today no feature sets
  one, so each relies on the chart's assignment rule, which sends a feature to every
  destination in its own ecosystem. That would fan pod logs out to both Loki-type
  destinations by itself. But an OTLP-type archive destination would also receive
  every OTLP feature it has signals enabled for, so the archive's coverage would
  depend on per-destination `metrics`/`logs`/`traces` flags rather than on a list
  anyone reads. Before the first rollout, confirm with `helm template` that each
  feature renders exactly one Cloud and one archive writer per backend.
- **Traces and tail sampling**: the sampler is a property of the `gc-otlp-endpoint`
  destination. The archive destination needs **the same policy list**, so we lift it
  into one module-level constant that both destinations reference. The probabilistic
  policy hashes the trace ID, so two samplers with the same policies keep
  near-identical sets of traces. What this costs is a second sampler collector per
  cluster (1–2 GiB, as sized in the comments). Sending unsampled traces to the
  archive is ruled out, because it would multiply trace storage several times over.
- **vector-log-proxy, EC2 Vector and EC2 Alloy**: add a second `loki` /
  `prometheus_remote_write` sink with the same `inputs` as the existing global
  funnel sink. The EC2 hosts only get it with their next AMI build.
- **Fleet Management**: pipelines pushed through Grafana Fleet Management
  (`remoteConfig`) are invisible to the archive unless they name an archive
  destination. Any long-lived pipeline belongs in `grafana.py` instead.

**2. Isolate failures.** Every archive writer gets bounded retries and a bounded
queue, and drops data once those are exhausted rather than pushing back on the
pipeline. By default, `loki.write` blocks when its send queue fills
(`queue_config.block_on_overflow` defaults to `true`, and the block is experimental),
and it retries 10 times (`max_backoff_retries`). A blocked archive writer can
therefore stall the shared `loki.process` stage that also feeds Cloud. Implementation
has to set non-blocking overflow on the archive writer, and supply it through the
chart's extra-config hooks if the chart does not expose it. Remote-write needs the
same treatment through its queue and WAL truncation settings. **Gate before each phase reaches Production:** scale the archive's
distributors to zero in QA for an hour, and confirm that Cloud ingest rate, collector
memory and pod-log lag don't change.

**3. Where the archive runs.** It runs in a new `observability-archive` namespace on
the **operations** EKS cluster in each archived environment, as a new project,
`applications/observability_archive/`. The cluster-level Alloy and k8s-monitoring
config stays in `substructure/aws/eks/grafana.py`. The archive is single-tenant per
environment, so the Production archive holds only Production data. That matches the
Cloud stack split and keeps a QA mistake from touching production history.

- **Deployment mode**: each system's official Helm chart, with zone-aware replication
  across 3 AZs. Production Loki at 119 GB/day is past monolithic mode's ~20 GB/day
  guidance, and simple scalable mode is deprecated and removed in Loki 4.0. That
  leaves Loki in **microservices (distributed) mode** at the low end of its sizing.
  For Mimir and Tempo (under 1M series and ~51 GB/day), pick between monolithic and
  distributed from each project's guidance when implementation starts.
- **Storage**: one S3 bucket per backend per environment, with S3 Intelligent-Tiering.
  Retention is enforced by each compactor (`retention_period` /
  `compactor_blocks_retention_period` / `block_retention`), and an S3 lifecycle rule
  backstops it at retention plus 7 days. Nothing goes to Glacier Flexible Retrieval or
  Deep Archive, since none of the three can read from it.
- **Access**: IRSA roles scoped to each bucket, with no static keys.
- **Network**: the archive's own S3 traffic uses the S3 gateway endpoint that `OLVPC`
  gives every VPC (`components/aws/olvpc.py`, attached to every route table), so it
  never touches NAT. Collectors in the other clusters reach the archive as described
  in step 4.

**4. Cross-VPC transport: an internal NLB over existing VPC peering.** The archive
runs in the operations VPC, while most telemetry comes from the applications, data and
residential clusters, each in its own VPC. Every one of those VPCs is already peered
with its environment's operations VPC. On 2026-10-08 every route table that has
subnets carried the route in both directions, in QA (`pcx-0477bd0342e80bd3b`,
`pcx-02d18f805a599c248`, `pcx-045c80780feadb2b0`) and in Production
(`pcx-0a5790366a86d3c20`, `pcx-066336acfe6e717ea`, `pcx-0cc05c86c12ff1529`).
EKS pods have VPC IPs, so an Alloy pod can open a connection straight to a private
address in the operations VPC.

The archive's write endpoints sit behind **one internal NLB** in the operations
cluster, with an internal Traefik or Gateway behind it routing `/loki/api/v1/push`,
`/api/v1/push` and OTLP to the three backends. This is the pattern StarRocks already
uses (`applications/starrocks/__main__.py`):

- `aws-load-balancer-scheme: internal` with IP targets.
- A security group that admits only the peered VPC CIDRs.
- An external-dns record in the public `ol.mit.edu` zone that resolves to the NLB's
  private IPs. This works even though DNS resolution across the peering connections
  is disabled.

Collectors push to that hostname over HTTPS with per-environment credentials. Nothing
about this path is internet-reachable.

Transport options compared, using ~5 TB/month of production telemetry and us-east-1
list prices (AWS Price List API, 2026-10-08; free same-AZ peering is from AWS's
2021 peering pricing change):

| Option | Path | Per-GB charges | ≈ / month |
|--------|------|----------------|-----------|
| **Internal NLB over peering (chosen)** | pod → peering → internal NLB → archive | Peering: free within an AZ, $0.01/GB each side across AZs. NLB: $0.0225/hr + $0.006/LCU-hr (1 LCU = 1 GB/hr) | ~$65 cross-AZ (assuming ⅔ of connections cross AZs) + ~$45 NLB ≈ **$110** |
| PrivateLink (endpoint service + an interface endpoint in each source VPC) | pod → interface endpoint → NLB → archive | $0.01/GB processed + $0.01/hr per AZ per endpoint | ~$50 + ~$65 (3 VPCs × 3 AZs) + NLB ≈ **$160** |
| Transit Gateway | pod → TGW → archive | $0.02/GB processed + $0.05/hr per attachment | ~$100 + ~$145 (4 attachments) ≈ **$245** |
| Public endpoint (rejected) | pod → NAT → public LB IP → archive | NAT $0.045/GB + $0.01/GB each way via public IPs | ~$225 + ~$100 ≈ **$325+** |

PrivateLink only earns its cost when peering is unavailable or CIDRs overlap, and
neither applies here. A Transit Gateway adds charges to a mesh that peering already
provides. The public endpoint never leaves AWS, so it avoids internet egress pricing,
but it still pays NAT processing and public-IP transfer on traffic we send to
ourselves. Collector configs must therefore never point at a public archive hostname.
A post-rollout check confirms this: archive traffic must not appear in the source
VPCs' NAT `BytesOutToDestination`.

Assumptions behind the numbers:

- The GB figures are Grafana Cloud's received-bytes metrics. Loki pushes are
  snappy-compressed and OTLP is gzip-compressed, so the bytes on the wire should be
  lower, and every per-GB figure here is an upper bound until QA measures actual
  transfer.
- Cross-AZ spend can be cut further by pinning collectors to the NLB's per-AZ DNS
  names, or by turning off cross-zone load balancing. At these amounts that tuning is
  deferred.

**5. Query path.** We run one PDC agent per archived environment, in the archive
namespace, and add datasources to the matching Cloud stack under clear names:
`Loki (archive, 13mo)`, `Mimir (archive, 13mo)` and `Tempo (archive, 90d)`. The
datasources and the PDC network are declared in Pulumi alongside the existing Grafana
Cloud resources. Archive datasources are **excluded from alert rules**, enforced by
review and by a lint in `infrastructure/grafana_alerting`.

**6. The archive monitors itself in Cloud.** The archive's own metrics and logs ship
to Grafana Cloud through the normal pipeline. Two ticket-tier alerts cover it: archive
writer drop rate above zero, sustained (the `*_dropped_*` counters on the archive
writers), and compactor or retention failure. Neither ever pages.

## Consequences

### Positive Consequences

- Production logs and metrics become queryable for 13 months instead of 31 days,
  from the same Grafana Cloud UI and with the same queries.
- Data we keep long-term lives in our own account, under our own retention and
  deletion policy, independent of the Grafana Cloud contract.
- It sets up Option 4, using the archive to cut Cloud ingest spend, if we want it
  later.

### Negative Consequences

- **We own three more stateful systems**, with chart upgrades, ring and compactor
  failure modes, and S3 cost to watch. Ownership has to be explicit: the
  infrastructure team owns it, with a recurring upgrade task (quarterly at minimum)
  on the team's board, or the archive rots.
- **Collectors get heavier and more complex.** There are more writers per collector
  and a second tail sampler per cluster, and `grafana.py` is already heavily
  tuned. Every change to sampling policy or processing now
  has two consumers.
- **Keeping data longer widens our privacy and compliance exposure.** Logs contain
  learner identifiers, IP addresses and request paths. Thirteen-month retention of
  those needs sign-off. We also need a working deletion process: Loki's delete API,
  enabled on the compactor, for erasure requests.
- **Derived Cloud data is missing**: span metrics, service graphs, Adaptive Metrics
  aggregations and Cloud recording rules. Any long-term trend we care about has to be
  re-expressed as a recording rule in mimir-archive, or computed at query time.
- **No backfill.** History starts on the day each phase is turned on. Exporting the
  31 days held in Cloud is possible (`logcli query` over the range) but is not planned.

### Neutral Consequences

- Rough costs at steady state with 13-month logs and metrics and 90-day traces
  (order of magnitude, to be checked in QA before Production):

  | Item | Estimate / month | Basis |
  |------|------------------|-------|
  | Loki storage | $60–220 | 119 GB/day raw, 5–10× compression, 395 days, Intelligent-Tiering vs Standard |
  | Mimir storage | $15–25 | 8.1k samples/s at ~1.5–2 B/sample + index |
  | Tempo storage | $20–40 | 51 GB/day, 90 days |
  | Compute | $700–1,200 | ~20–30 vCPU / 80–120 GiB total across the three, RF3, on-demand Graviton; less with the Compute Savings Plan |
  | Cross-AZ replication | $150–250 | RF3 on ~5 TB/month ingest |
  | Cross-VPC transport | ≤ $110 | Internal NLB over peering (step 4) |
  | **Total** | **~$1,050–1,800** | Option 2 removes roughly $250–400 of this |

  S3 request charges are left out, because they are small at this write rate, but
  they should be checked against real bills after the first month.
- The implementation adds a new Pulumi project, a new S3 bucket set per environment,
  new IRSA roles and a PDC token in SOPS.

## Implementation Notes

- **Effort Estimate:** Phase 1 (logs plus PDC plus datasource): ~1.5–2 weeks. Phase 2
  (metrics): ~1 week. Phase 3 (traces plus second sampler): ~1–1.5 weeks. Each phase
  goes to QA first and runs there for at least a week before Production.
- **Risk Level:** Medium. The main risk is the archive leaking back-pressure into the
  Cloud stream, which the failure-isolation gate in step 2 exists to catch. The
  second-highest is mis-rendered chart destinations sending data twice to Cloud,
  which would raise the Cloud bill. Watch `grafanacloud_*_bytes_received` after every
  rollout.
- **Dependencies:** Grafana Cloud PDC availability on our plan (expected, to confirm),
  the retention and privacy sign-off, and a pinned Alloy version recent enough for the
  writer settings above.
- **Migration Path:** Nothing is migrated. Rolling back means removing the archive
  destinations and deleting the stack. Cloud never notices.

## Related Decisions

- ADR-0007 (multi-tenant ClickHouse on data EKS): precedent for running a stateful
  analytics store on EKS.
- ADR-0012 (StarRocks): the engine Option 3 would query through.
- `src/ol_infrastructure/substructure/aws/eks/ALLOY.md`: current pod-log label
  pipeline.

## References

- Loki deployment modes (SSD deprecation, monolithic sizing): https://grafana.com/docs/loki/latest/get-started/deployment-modes/
- Grafana Private Data Source Connect: https://grafana.com/docs/grafana-cloud/connect-externally-hosted/private-data-source-connect/
- k8s-monitoring chart destinations: https://github.com/grafana/k8s-monitoring-helm/tree/main/charts/k8s-monitoring/docs
- Loki retention and deletion: https://grafana.com/docs/loki/latest/operations/storage/retention/
- Mimir sizing: https://grafana.com/docs/mimir/latest/manage/run-production-environment/planning-capacity/
- Tempo sizing: https://grafana.com/docs/tempo/latest/operations/

## Notes

Open questions for reviewers:

1. **Retention targets.** Are 13 months for logs and metrics and 90 days for traces
   right? Is there an institutional records-retention requirement (MIT IS&T, or
   FERPA-related) that sets a floor or a ceiling for logs?
2. **QA archive.** QA is 5% of production log volume, but its 225k series are over
   half of production's. Archiving it would add about half again to the Mimir footprint for
   little value. Proposal: production only.
3. **Phase 3 go/no-go.** Who decides, after phase 2, whether traces earn their place,
   and on what evidence (for example, the number of times an investigation needed a
   trace older than 31 days)?
4. **Option 5 quote.** Get one before acceptance, so the comparison uses a real
   number.

---

**Review History:**

| Date | Reviewer | Decision | Notes |
|------|----------|----------|-------|
| 2026-10-08 | AI agent | Drafted | Created by AI agent, pending human approval |

**Last Updated:** 2026-10-08
