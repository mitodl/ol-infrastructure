# 0012. Replace Trino/Starburst Galaxy with StarRocks as the Sole Query Engine

**Status:** Accepted
**Date:** 2026-09-25
**Amended:** 2026-11-20 (batch-cluster trigger in decision 3; status of the stale-read
cost in decision 1; soft-isolation consequence)
**Deciders:** tmacey, Data Platform team
**Technical Story:** `wp-trino-starburst-galaxy-starrocks-migration-decom-62b677` /
`tk-e1-architecture-decisions-and-adr-for-the-trino--6f3075`

## Context

### Current Situation

The Starburst Galaxy agreement will not be renewed. Project scoping puts its expiry at
the end of February 2027, while the FY27 budget draft books Galaxy at $72K/yr through
January 2027. The exact end date and notice period are being confirmed
(`tk-confirm-the-starburst-contract-end-date-notice-p-678ca1`). The milestones below
carry a schedule for each date, and confirming it is a gate before M2.

StarRocks 4.1.4 (operator/chart 1.11.7) already runs in CI, QA and Production on the
data EKS clusters in shared-data mode (`applications/starrocks`,
`substructure/starrocks`). It has Vault dynamic credentials (`database-starrocks`
mount), Keycloak OAuth2 and JWT security integrations, and Glue-backed Iceberg
external catalogs (`ol_data_lake_qa`, `ol_data_lake_production`). Superset,
ol-analytics-api, the MIT Learn warehouse pull and the 8 `b2b_analytics`
materialized views already query it.

Still on Galaxy: the dbt warehouse (668 of 676 models, dbt-trino), Dagster's Iceberg
layer maintenance, Superset's charts and dashboards, OpenMetadata ingestion and
lineage, notebooks, mitxonline's `migrate_edx_data`, and the Galaxy IaC, IAM trust
role and static credentials.

Ingestion (dlt/Airbyte → pyiceberg → Glue) never touches Trino, so the lake itself
does not move. The question is which engine builds and serves the dbt layers on top of
it, and what shape the StarRocks deployment has to take to be the only one.

### Constraints

- Galaxy access must be revoked, and its IaC removed, at least two weeks before the
  confirmed contract end date.
- StarRocks here is the open source build. Enterprise-only features are not options.
- The Lakekeeper/Gravitino Iceberg REST catalog work
  (`wp-starrocks-iceberg-rest-catalog-jwt-identity-dele-13ccbe`) is in flight on its
  own schedule. Coupling the cutover to it would put the contract deadline behind a
  project with no committed date.

## Decisions

### 1. dbt-built layers stay Iceberg tables in Glue

StarRocks writes every dbt-built layer (staging, intermediate, dimensional, marts,
reporting) as Iceberg through the Glue external catalog, using dbt-starrocks
`config(catalog=..., database=...)`. Native `default_catalog` tables are only for
serving materialized views such as `b2b_analytics`.

A native-table warehouse would be invisible to pyiceberg, IRx, DuckDB and Trino. Its
recovery would rest on Cluster Snapshot, which covers FE metadata and data in
shared-data mode but is beta and not configured in our IaC, so it would be new
snapshot and restore work to build and validate. It would also have no parallel-run
path against the Trino build. `b2b_analytics` already demonstrates the invisibility:
it exists in `default_catalog` and is absent from Glue.

The storage-model spike on QA (2026-09-21, recorded in witan memory
`pf-starrocks-writes-to-glue-iceberg-work-views-sche-210378`) verified the following:

- CTAS, identity partitioning equivalent to Trino's `ARRAY['platform']`, DELETE and
  grants all work.
- pyiceberg reads the results back with partition spec, types and snapshot history
  intact.

We accept these costs, each tracked:

- Views cannot be created in a Glue-type Iceberg catalog. `IcebergCatalog#getViewBuilder`
  is overridden only by the Hive and REST catalog types. The 29 view-materialized
  models become tables for the cutover
  (`tk-convert-the-29-view-materialized-models-to-table-01308c`). We revisit that only
  if the Gravitino Glue backend implements `ViewCatalog`
  (`tk-confirm-whether-gravitino-s-custom-gluecatalog-b-45a25e`).
- dbt cannot create a schema in the catalog: located `CREATE DATABASE` 403s on
  `getFileStatus`, and dbt-starrocks emits the location-less form.
  (`tk-fix-iceberg-catalog-schema-creation-on-starrocks-88f610`,
  `tk-add-starrocks-create-schema-emitting-the-layer-s-dd41e7`)
- A read immediately after `INSERT OVERWRITE` can see the previous snapshot for up to
  ~10s. `ol-dbt run` wraps `dbt build`, so a model's tests can run against stale data
  until this is fixed (`tk-make-starrocks-iceberg-reads-see-their-own-write-802810`).
  That task closed on 2026-10-15 as not reproducible. Stale reads after another
  engine's commit moved to `tk-starrocks-reads-a-stale-iceberg-snapshot-for-30s-0cb47c`,
  and the Glue catalogs now run with the Iceberg metadata cache off. The Gravitino
  catalog (`ol_governed_lake_*`, enabled in QA and Production) does not set
  `enable_iceberg_metadata_cache`, so a read through it can still see the previous
  snapshot after another engine commits. With the cache on, QA's Glue catalog stayed
  stale for 16.9-58.0s per FE.
- `starrocks__drop_relation` omits `FORCE`, which orphans data files on every full
  refresh and incremental temp relation
  (`tk-override-starrocks-drop-relation-to-emit-drop-ta-46824b`).
- `INSERT OVERWRITE` is partition-scoped on Iceberg, and dbt-starrocks has no
  `merge` or `delete+insert`. The 21 delete+insert models need a different strategy
  (`tk-port-the-21-delete-insert-incremental-models-11--c9c109`).

The spike's findings are filed upstream as seven issues: StarRocks/dbt-starrocks#123
through #128 for the adapter, and StarRocks/starrocks#79448 for Glue view support. The
adapter's external-catalog support landed days before the spike, so the adapter
issues may close upstream. The plan does not depend on that.

### 2. Glue remains the write catalog through the cutover

Nothing in this migration moves the catalog. The Iceberg REST catalog work stays a
separate project. If it lands before M3, StarRocks can switch catalog type as its own
change.

### 3. One StarRocks cluster per environment, isolated with resource groups

Galaxy splits interactive traffic (`ol-data-interactive`: Superset, mitxonline) from
batch (`ol-data-lake-production`: dbt, OpenMetadata, notebooks). StarRocks OSS 4.1
cannot reproduce that split inside a cluster the way Galaxy or StarRocks enterprise
can (verified on `branch-4.1` at a4d9ee01 and operator `v1.11.7`):

- `WarehouseManager` stubs `CREATE`/`ALTER`/`DROP WAREHOUSE` with
  `"Multi-Warehouse is not implemented"`.
- `DefaultWarehouse` stubs every CN group operation with `"CnGroup is not implemented"`.
- The operator's `StarRocksWarehouse` CRD is documented as enterprise-only, and
  `StarRocksCluster` has a single `starRocksCnSpec`.

The two real options were one cluster with resource groups, or a second
`StarRocksCluster` per environment. A second cluster doubles the FE quorum
(3 × 24Gi FE), the Iceberg catalogs, the Vault mount, the OIDC integrations and the
grants. No load data justified that at the time. Over the 30 days to 2026-09-25, production
CN stayed at its minimum of 2 pods and peaked at 1.65 cores of 32 requested.

We keep one cluster per environment and isolate with:

- An interactive resource group for Superset, ol-analytics-api, mitxonline and
  notebooks, classified by user/role. It holds `exclusive_cpu_cores` (or
  `exclusive_cpu_percent`) and a `concurrency_limit`.
- A batch resource group for dbt, OpenMetadata and Iceberg maintenance. It started
  with `mem_limit` and big-query limits so one runaway model could not evict
  interactive queries. Since 2026-11-16 it has no memory cap below the CN
  (`mem_limit` 1.0, no big-query limit). Those caps refused models QA has to build,
  and StarRocks has to run the builds Trino runs. A cap comes back once a measured
  conflict with interactive queries shows the need.
- Query queues (`enable_query_queue_select`, set with `SET GLOBAL`, because
  `ALTER WAREHOUSE` hits the multi-warehouse stub on OSS).

Exclusive cores are capped at the smallest CN's core count minus one, so CN pods must
stay uniformly sized.

The trigger to revisit is measured, not assumed. The response depends on which
trigger fires:

- If the M2 production parallel run shows interactive latency regressing during the
  nightly build (ol-analytics-api has a 30s query timeout), and resource-group tuning
  cannot fix it, we add a second cluster for batch. The scale spike
  (`tk-scale-spike-build-the-tracking-log-scale-staging-fe1438`) and the M2 run supply
  that data.
- If a batch query takes a production CN to its process memory limit, we revisit
  whether to add a second cluster for batch. Part of the memory that gets there is
  not charged to the query, so a per-query cap does not stop it (see below).

#### A batch query can exhaust a CN's process memory (added 2026-11-20)

The second trigger fired while porting `tfact_problem_events` to StarRocks
(`tk-tfact-problem-events-starrocks-incremental-runs--fdfc5a`). Production then had
2 CNs with 16Gi containers, a StarRocks process limit of 13.9 GB and a query pool of
12.52 GB each. The incremental and the profiling runs that followed it failed five
times:

| Time (UTC) | CN | Error | Cause |
|------------|----|-------|-------|
| 2026-11-19 ~21:50 | cn-0 | `5609 Memory of process exceed limit`, 49.7s in, default session settings | unpruned scan |
| 2026-11-19 ~22:00 | cn-0 | the same, 33.0s in, with `query_mem_limit` 8 GiB and spill on | unpruned scan |
| 2026-11-20 14:47 | cn-0 | process limit, a profiling probe of the new-events path | unpruned scan |
| 2026-11-20 15:09 | cn-0 | `Memory of query_pool exceed limit` at 12.52 GB, the full incremental (84.6s in) | not profiled |
| 2026-11-20 ~15:11 | cn-1 | process limit, profiling the stale-key re-check at `pipeline_dop` 4 | `dim_user` hash join |

The times come from the session records. `starrocks_be_process_mem_bytes` catches two
of the five windows: since 2026-11-16 its only samples above 9 GB are 2026-11-19
21:38-21:50 and 2026-11-20 15:09. The others fell between 30-60s scrapes. The
21:38-21:46 samples come before the first recorded failure, and what used that memory
is not identified.

Two mechanisms, from the query profiles:

- Unpruned scan. A watermark filter was not pushed into the Iceberg scan, so the scan
  read every historical problem event with its JSON columns. On the new-events path
  the query's tracked memory peaked at 2.09 GB per node while cn-0's process reached
  13.9 GB, so most of that scan memory was not charged to the query. An 8 GiB
  `query_mem_limit` did not stop it (the ~22:00 run). A resource-group `mem_limit` or
  `big_query_mem_limit` was not tested, because production's batch group has neither
  below the CN. We expect the same result, since both are measured against the
  query's charged memory, but that is unverified.
- Join memory that grows with dop. The stale `user_fk` re-check built a `dim_user`
  hash table of about 1 GB per CN at dop 1 and 4.23 GB at dop 4, then ran cn-1 out of
  process memory. That memory is charged to the query, so a query or group cap below
  the process limit is expected to refuse it first. Neither was tested on this query.

The 15:09 failure hit the query pool, not the process limit. The query pool counts
charged memory, and a resource group's `mem_limit` is a fraction of that same pool,
so a group cap is expected to have refused that query earlier (also untested). That
narrows the problem: the failures resource groups are not expected to reach are the
process-limit ones driven by uncharged scan memory.

ol-data-platform#3451 fixed this model with a literal date floor on the scans, a
rewrite of the stale-key re-check, and `pipeline_dop` 2. Its incremental now finishes
in 83.5s on a 159.8M-row production copy. Nothing stops the next model with an
unpushed filter from doing the same.

The CN autoscaler cannot react either. It scales on CPU (80% of a 4-core request)
with a 300s scale-up stabilization window, so it acts on the lowest replica
recommendation of the last 300s and adds a CN only once CPU has stayed above target
for that long. cn-0 did reach 5.5-7.2 cores during the 2026-11-20 runs, but each
query failed within 90s. A new CN would also not take work off one
already running a query's fragments, because StarRocks assigns fragments to CNs when
it schedules the query.

What the failures did to other work, as far as it can be seen without the FE audit
log:

- `starrocks_fe_query_err` rose by about 2 in each 2-minute window that holds a
  failure, against 16-74 queries per 2 minutes cluster-wide. One error per window is
  not accounted for.
- No StarRocks pod restarted (`kube_pod_container_status_restarts_total`, 30 hours).
  A 5609 is StarRocks refusing allocations, not a kernel kill.
- ol-analytics-api had no production errors in Sentry over 2026-11-18 to 2026-11-20.
  All 31 events in that period are from CI.
- Unverified: whether any Superset, ol-analytics-api, MIT Learn or mitxonline query
  ran on those CNs at those moments. Only `fe.audit.log` on the FE pods records the
  user and resource group per query, and the builtin audit logger writes no table.

So no interactive failure is on record, which is weak evidence at 8-37 queries a
minute cluster-wide, before Superset's charts move over. While a CN is at its
process limit, any query that allocates on it fails, whatever its resource group.

Whether to add the batch cluster before M3 is not decided. A split has to settle
these, on top of the doubled FE quorum, catalogs, Vault mount, OIDC integrations and
grants listed above:

- Freshness across clusters. The Glue catalogs (`ol_data_lake_*`) set
  `enable_iceberg_metadata_cache` off (`substructure/starrocks`), so a serving cluster
  reads a batch cluster's commit on its next query. The Gravitino catalog does not
  set it, so a split that serves through Gravitino needs the same setting.
- Where StarRocks-internal objects live. The `b2b_analytics` and
  `b2b_learner_records` materialized views exist only in the `default_catalog` of the
  cluster that built them, and every `dbt build` drops and recreates them. On the
  serving cluster, their rebuilds and refreshes are batch work there
  (`tk-route-b2b-analytics-materialized-view-refreshes--521f9c`).
- A second host per environment in `bin/starrocks-auth`, the dbt profiles and
  Dagster's `STARROCKS_HOST_MAP`, with its own Vault role.

This narrows two related tasks. The resource group definitions
(`tk-define-starrocks-interactive-batch-resource-grou-d3112c`) are expected to bound
the charged memory (join and query-pool failures) but not unpruned scan memory, so
tuning them answers part of this trigger. Testing a group cap against both is part of
that task. Workload-signal CN scaling
(`tk-scale-starrocks-cns-on-workload-signals-keda-bui-401b1d`) adds CNs but does not
move a running query off a full one.

### 4. CI gets its own queryable lakehouse

Today the CI StarRocks cluster runs with `enable_data_lake_integration: "false"` and
OIDC off. It exists only so ol-analytics-api's CI deploy can obtain Vault
credentials. Galaxy never served CI either.

We enable the lake integration on CI, against a CI lake rather than QA's. The
`data_warehouse` CI stack already creates the `ol-data-lake-*-ci` buckets and
`ol_warehouse_ci_*` Glue databases, but nothing registers them as a catalog.

The access rule in `lib/aws/iam_helper.py` cannot express this as written.
`DATA_LAKE_ENVIRONMENTS` lists only `qa` and `production`, and
`readable_data_lake_environments` grants an engine every lake not in
`PROTECTED_DATA_LAKE_ENVIRONMENTS`, with full read/write through the shared
query-engine policy. Two naive changes both go wrong:

- Flipping the CI flag alone registers `ol_data_lake_qa` on the CI cluster with write
  access. That is the cross-tier exposure #6023 removed for QA → Production.
- Adding `ci` to `DATA_LAKE_ENVIRONMENTS` alone makes QA _and_ Production register
  `ol_data_lake_ci` too, with CI's read/write policy attached to their roles.

So the implementation changes the rule, not only the tuple:

- CI becomes a leaf tier. Its own engine registers `ol_data_lake_ci` with write access,
  and no other environment's engine registers it.
- A new read-only query-engine policy lets the CI engine read the QA lake, so slim data
  CI can `--defer` unchanged upstream models to QA relations while writing its own
  subgraph to CI. QA gains nothing.
- `applications/starrocks` gets a `ci` entry in `_DATA_LAKE_STACK_NAMES`, and the CI CN
  (currently 1 core / 2Gi, fixed at one replica) is sized for real builds.

This moves credentialed build+test work off QA. The work covered is slim data CI
(`tk-p2-slim-data-ci-build-test-changed-subgraph-conc-6a6ed7`), the per-adapter macro
dispatch harness (`tk-cross-engine-smoke-build-for-src-ol-dbt-macros-c-712d89`) and
Concourse's image-build `dbt ls`. Per-PR schemas no longer compete with QA's nightly
build for CN capacity or clutter QA's Glue databases.

The cost is one more lake tier to keep alive. The CI lake is empty, so anything not
deferred to QA needs seeds or fixtures. GitHub Actions stays credential-free on
DuckDB, as decided for the data-trust project on 2026-07-13.

### 5. Human and application access goes through StarRocks' own auth

- People connect with Keycloak OIDC (`keycloak_oauth2` for JDBC/SQL clients,
  `keycloak_jwt` for token-bearing clients). `bin/starrocks-auth` in ol-data-platform
  implements the browser, CI and Vault flows. Galaxy rejected externally-issued JWTs;
  StarRocks accepts them.
- Services use Vault dynamic credentials from the `database-starrocks` mount.
- Static Trino credentials in SOPS are deleted at decommission, not migrated.

### 6. Scale is not retired by this ADR

QA holds hundreds of rows per staging table, so M1 (full warehouse green on QA) proves
correctness only. Tracking-log-scale models have never run on StarRocks. That risk
stays open until the scale spike and the M2 production parallel run. The parallel run
also assumes Trino can read StarRocks-written Iceberg tables, which is unverified
(`tk-verify-trino-can-read-starrocks-written-iceberg--1eb5e2`).

## Milestones

The contract end date gates the schedule. It must be confirmed before M2. If the
agreement ends 2027-02-28, the February column applies. If it ends 2027-01-31, M3 and
M4 move to the January column, which keeps the same two-week buffer.

| Milestone | Feb 28 end | Jan 31 end | Exit condition |
|-----------|------------|------------|----------------|
| M0 | 2026-09-25 | 2026-09-25 | This ADR |
| M1 | 2026-10-31 | 2026-10-31 | Full warehouse green on QA StarRocks |
| M2 | 2026-11-30 | 2026-11-30 | Production parallel run; QA consumers repointed; contract end date confirmed |
| M3 | 2027-01-15 | 2026-12-18 | All production consumers on StarRocks; Trino build disabled |
| M4 | 2027-02-13 | 2027-01-15 | Galaxy access revoked; Galaxy IaC, IAM trust role and secrets removed |

## Out of Scope

- The Iceberg REST catalog (Lakekeeper/Gravitino) and per-user identity delegation to
  the lake.
- The ingestion write path (dlt/Airbyte → pyiceberg → Glue), which does not use Trino.
- Semantic accuracy and consolidation of the dbt models themselves
  (`wp-dbt-warehouse-audit-semantic-accuracy-consolidat-b4244e`).

## Consequences

### Positive Consequences

- One engine, one credential system and one set of grants replace Galaxy's RBAC and
  static credentials, and the $72K/yr Galaxy cost ends in FY28.
- The warehouse stays Iceberg in Glue, so pyiceberg, IRx, DuckDB and a future REST
  catalog keep working without depending on StarRocks.
- Notebooks and published apps get working credential paths that Galaxy could not
  offer.
- Credentialed CI stops sharing QA.

### Negative Consequences

- The 29 view models become tables, which adds build time and storage.
- Several dbt-starrocks gaps have to be carried as local macro overrides until
  upstream fixes land.
- Isolation is soft. A resource group bounds CPU and the memory charged to its
  queries. Scan memory a query isn't charged for took a whole CN to its process limit
  on 2026-11-19 and 2026-11-20 (see the batch-cluster trigger in decision 3). A cluster-wide FE
  or shared-data incident also takes out interactive and batch together, where
  Galaxy's split would have contained it.
- CI becomes a third lake tier with its own IAM, catalog and data-seeding story.

### Neutral Consequences

- Follow-up work: resource group and query queue definitions in
  `substructure/starrocks`; CI lake enablement; the ol-data-platform documentation
  that still describes Trino as the query engine.
