# 0012. Replace Trino/Starburst Galaxy with StarRocks as the Sole Query Engine

**Status:** Accepted
**Date:** 2026-09-25
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
grants. No load data justifies that yet. Over the 30 days to 2026-09-25, production
CN stayed at its minimum of 2 pods and peaked at 1.65 cores of 32 requested.

We keep one cluster per environment and isolate with:

- An interactive resource group for Superset, ol-analytics-api, mitxonline and
  notebooks, classified by user/role. It holds `exclusive_cpu_cores` (or
  `exclusive_cpu_percent`) and a `concurrency_limit`.
- A batch resource group for dbt, OpenMetadata and Iceberg maintenance, with
  `mem_limit` and big-query limits so one runaway model cannot evict interactive
  queries.
- Query queues (`enable_query_queue_select`, set with `SET GLOBAL`, because
  `ALTER WAREHOUSE` hits the multi-warehouse stub on OSS).

Exclusive cores are capped at the smallest CN's core count minus one, so CN pods must
stay uniformly sized.

The trigger to revisit is measured, not assumed. If the M2 production parallel run
shows interactive latency regressing during the nightly build (ol-analytics-api has a
30s query timeout) and resource-group tuning cannot fix it, we add a second cluster
for batch. The scale spike (`tk-scale-spike-build-the-tracking-log-scale-staging-fe1438`)
and the M2 run supply that data.

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
- Isolation is soft. A resource group bounds CPU and memory, but a cluster-wide FE or
  shared-data incident takes out interactive and batch together, where Galaxy's split
  would have contained it.
- CI becomes a third lake tier with its own IAM, catalog and data-seeding story.

### Neutral Consequences

- Follow-up work: resource group and query queue definitions in
  `substructure/starrocks`; CI lake enablement; the ol-data-platform documentation
  that still describes Trino as the query engine.
