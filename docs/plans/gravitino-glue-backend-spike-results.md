# SPIKE RESULT: Gravitino's Iceberg REST service over the existing Glue tables

Task: `tk-spike-can-gravitino-s-iceberg-rest-service-front-174227`
Date: 2026-09-18
Verdict: **Option A passes.** Gravitino 1.3.0 fronts pre-existing Glue Iceberg tables through a
`custom` `GlueCatalog` backend, authorizes per user on them, and shares Glue's optimistic commit
protocol with writers that go to Glue directly. One StarRocks defect found, which rules out writes
through StarRocks with per-user identity. Credential vending on a Glue-backed table is not measured
(item 5).

## Harness

The same local harness as `gravitino-spike-results.md`, pointed at real AWS for the catalog:

| Component | Version |
|---|---|
| StarRocks | `4.1.3-8a8e186` (allin1-ubuntu, matches deployed) |
| Gravitino | `1.3.0`, stock `apache/gravitino` image, IRC as auxiliary service, dynamic config provider |
| Keycloak | `26.0.8` |
| MySQL client | `9.4.0` |
| Catalog | Real AWS Glue, a throwaway database pair and S3 bucket in `us-east-1` |
| Direct writer | PyIceberg `0.10.0` `GlueCatalog`, standing in for Airbyte/dbt/Dagster |

Two throwaway Glue databases, each seeded **directly through PyIceberg's `GlueCatalog` before
Gravitino ever saw them**: `<db>` with `enrollments` and `grades`, and `<db>_b` with `untouched`.
`<db>_b` was created after Gravitino was running and was never listed or loaded by anything before
the grant test, so item 3 cannot have been helped by an earlier import.

Users as before: `alice` in group `ol_data_analyst`, `bob` in `ol_data_engineer`. Roles granted to
groups only:

- `analyst_read`: `USE_CATALOG`, `USE_SCHEMA` on `<db>_b`, `SELECT_TABLE` on `<db>_b.untouched`.
- `engineer_rw`: `USE_CATALOG`, `USE_SCHEMA` on `<db>`, `SELECT_TABLE` + `MODIFY_TABLE` on
  `<db>.enrollments`.

Gravitino and StarRocks got AWS access from a 12h `GetSessionToken` credential in the container
environment. No IAM role could be created for this run, which is why item 5 is open.

## Results

| # | Item | Result |
|---|---|---|
| 1 | `custom` + `GlueCatalog` loads in the stock image; is `table-metadata-cache-impl=""` required | **PASS**; not required |
| 2 | Through StarRocks `security=JWT`: list/load/select, group-granted, allow and deny | **PASS for reads.** Writes run as the bootstrap principal (defect below) |
| 3 | Grant on a pre-existing Glue schema/table with no manual import | **PASS** |
| 4 | Concurrent writes through Gravitino and straight to Glue on one table | **PASS**, conditional on the Glue table version on both sides, no lost commit |
| 5 | Credential vending for a Glue-backed table | **NOT MEASURED**, needs an IAM role; move to QA |

### Item 1: the backend loads, and the cache setting is optional

The Glue SDK is not in `iceberg-rest-server/libs` or `catalogs/lakehouse-iceberg/libs` in the
image. Only `iceberg-aws-1.11.0.jar` is, which has `GlueCatalog` but not `GlueClient`. The SDK
lives in `iceberg-bundles/gravitino-iceberg-aws-bundle-1.3.0.jar`, and the image entrypoint symlinks
every jar under `iceberg-bundles/` into both lib directories at container start
(`dev/docker/gravitino/docker-entrypoint.sh:52-63`). **The chart must keep exec'ing that
entrypoint**, which the deployment spec already requires for `SKIP_CONFIG_REWRITE`, or the backend
cannot load.

Catalog properties that worked:

```properties
catalog-backend = custom
catalog-backend-impl = org.apache.iceberg.aws.glue.GlueCatalog
uri = https://glue.us-east-1.amazonaws.com
warehouse = s3://<bucket>/wh
client.region = us-east-1
```

`uri` is a required property for every `lakehouse-iceberg` catalog
(`IcebergCatalogPropertiesMetadata.java:75-76`) and `GlueCatalog` ignores it, so it takes a
placeholder. The Glue endpoint is the least misleading one.

`table-metadata-cache-impl=""` is **not** required. Without it, Gravitino logs once and disables the
cache (`IcebergCatalogWrapper.java:488-502`):

```
WARN IcebergCatalogWrapper.loadTableMetadataCache - Catalog 'custom' does not support the table
metadata cache, because the catalog impl does not support get metadata location. The cache is
disabled. Set 'table-metadata-cache-impl' to an empty string ("") ...
```

Set it anyway so the warning does not become noise, but it changes nothing.

The IRC listed the pre-existing namespace and loaded `enrollments` at the exact metadata file
PyIceberg had written (`.../enrollments/metadata/00001-15309854-....metadata.json`). The backend
was initialized 3 times across 129 audited operations, so it is cached per catalog, not rebuilt per
request.

One thing to carry into the IAM policy: as the service admin, both the management API and the IRC
listed **every Glue database the credential could see**, `airbyte_test_namespace`,
`ol_warehouse_ci_*`, `ol_data_lake_raw_qa` and so on. Users only see what they are granted (item 2),
but Gravitino's own view of Glue is whatever the pod role allows. That is what the per-environment
policy in the deployment spec's "The pod role" section is for. How Glue's `GetDatabases` behaves
under a policy restricted to some database ARNs was not tested.

### Item 2: StarRocks reads pass, StarRocks writes do not carry the user

`CREATE EXTERNAL CATALOG` with `security=JWT` and the bootstrap credential, as in the first spike,
plus `aws.s3.use_aws_sdk_default_behavior=true` and `iceberg.catalog.vended-credentials-enabled=false`
because nothing was vended in this run (item 5). StarRocks users got broad StarRocks grants
(`USAGE` on the catalog, `SELECT` and later `INSERT` on everything) so that Gravitino was the only
layer deciding.

Reads, in order, denials first so no cache was warm:

```
bob   SELECT <db>_b.untouched     ERROR 1064 Failed to get database using REST Catalog    (no USE_SCHEMA)
alice SELECT <db>.enrollments     ERROR 1064 Failed to get database using REST Catalog    (no USE_SCHEMA)
alice SHOW DATABASES FROM grav    <db>_b                                                  (only the granted one)
alice SHOW TABLES FROM <db>_b     untouched
alice SELECT <db>_b.untouched     10 x / 20 y                                             ALLOW
bob   SELECT <db>.enrollments     12 rows                                                 ALLOW
```

Then the cases the first spike could not show:

```
bob   SELECT <db>_b.untouched     after alice loaded it    Failed to load table   Gravitino 403 as bob
alice SELECT <db>.enrollments     after bob loaded it      Failed to load table   Gravitino 403 as alice
bob   SELECT <db>.grades          schema allowed, table not granted   Failed to load table   403
```

The cross-user case matters. After `alice` loaded `untouched` successfully, `bob`'s load of the same
table still went to Gravitino and was denied there, so a table one user has loaded is not served
from the FE cache to a user the catalog would refuse. This does not contradict the earlier
observation that the same user's repeated `SHOW DATABASES` never reaches the catalog. It narrows it:
per-table loads for a different user did reach the catalog in this run.

Gravitino's audit log attributes each of these to the end user, with
`User-Agent: StarRocks-Iceberg-Connector/4.1.3-8a8e186`.

**Writes.** `bob` holds `MODIFY_TABLE` on `enrollments`; `alice` holds only `SELECT_TABLE` on
`untouched`. Both `INSERT`s failed, and not for the reason they should have:

```
bob   INSERT <db>.enrollments  Forbidden: Current user service-account-ol-gravitino-machine doesn't
                               exist in the metalake ol_data_platform
```

The audit log for one `INSERT`:

```
bob                                   LOAD_TABLE        <db>.enrollments   SUCCESS
service-account-ol-gravitino-machine  UNKNOWN           null               FAILURE 403 GET .../tables/enrollments
```

Adding the machine principal to the metalake and granting it `SELECT_TABLE` (not `MODIFY_TABLE`) on
`untouched` moved `alice`'s `INSERT` one step further, to the commit, which was then refused
**to the machine principal**:

```
alice                                 LOAD_TABLE   <db>_b.untouched   SUCCESS
service-account-ol-gravitino-machine  LOAD_TABLE   <db>_b.untouched   SUCCESS   (x3)
service-account-ol-gravitino-machine  UNKNOWN      null               FAILURE 403 POST .../tables/untouched
  "User 'service-account-ol-gravitino-machine' is not authorized to perform operation 'updateTable'"
```

So StarRocks plans an `INSERT` as the user and **commits it as the bootstrap principal**. The cause
is in StarRocks, not Gravitino. `IcebergMetadata.finishSink` loads the table with
`getTable(new ConnectContext(), dbName, tableName)` (`IcebergMetadata.java:1997` at tag `4.1.3`).
A fresh `ConnectContext` has no auth token, so `IcebergRESTCatalog.buildContext`
(`rest/IcebergRESTCatalog.java:450-467`) returns `SessionContext.createEmpty()`, and the table's
operations, including the commit, bind to the catalog's default session, which is the bootstrap
credential. `CachingIcebergCatalog`'s background `reload` (`:140-146`) does the same. On `main` as
of today `finishSink` has gained a `ConnectContext context` parameter and still calls
`getTable(new ConnectContext(), ...)`. No upstream issue or PR found.

Consequences:

- **The bootstrap principal must hold no Gravitino grants, and should stay out of the metalake**, as
  the authorization spec already says. Grant it `MODIFY_TABLE` anywhere and every StarRocks user with
  a StarRocks `INSERT` grant writes there as the machine, with the machine's name in the audit log.
- **Writes through StarRocks are not per-user.** Under Option A, pipeline writers go to Glue
  directly, so nothing we plan depends on StarRocks writes. Say so in the authorization spec rather
  than leave it implied.
- **A refused `INSERT` leaves orphan data files.** StarRocks BE writes Parquet to the table's
  `data/` prefix with its own S3 access before the FE commit is refused. After the denied `INSERT`s
  the prefixes held new, unreferenced files such as
  `enrollments/data/01a0b655-98e9-7a2c-bd20-86c199d6f55f_0_0_0.parquet`. Orphan-file cleanup
  (`remove_orphan_files`) catches them; the real point is that Gravitino's `MODIFY_TABLE` does not
  stop StarRocks from writing objects under a table's location. The StarRocks pod role's S3 write
  scope does.

The project description's line that "cache-missing reads and writes do carry user identity" came
from Lakekeeper's `create_namespace`. It holds for DDL that goes through `buildContext`. It does not
hold for `INSERT`.

### Item 3: grants on pre-existing Glue objects need no import

A role naming `<db>_b` and `<db>_b.untouched`, objects Gravitino had never loaded, was created and
granted to the group in one call each:

```
BASELINE            alice listNamespaces=403   alice loadTable(untouched)=403
                    bob   listNamespaces=403   bob   loadTable(untouched)=403
AFTER GROUP GRANT   alice listTables(<db>_b)=200   loadTable(untouched)=200
                    bob   listTables(<db>_b)=403   loadTable(untouched)=403
                    alice loadTable(<db>.enrollments)=403   (not granted)
```

Negative control: a role on `<db>_b.does_not_exist` is refused with
`400 IllegalMetadataObjectException`, so the existence check does reach Glue rather than passing
anything through. This confirms the call chain the spec inferred (`MetadataObjectUtil` to
`tableExists` to `loadTable` and import). The grant reconciler can grant on Glue objects by name with
no sync step.

### Item 4: both paths commit conditionally on the Glue table version

Three threads appending through Gravitino as `bob` (PyIceberg `RestCatalog`) raced three threads
appending straight to Glue (PyIceberg `GlueCatalog`), 8 appends each, one row per append, on
`enrollments`:

```
successful appends: rest=8 glue=1 total=9
failures: 39, all CommitFailedException
main ancestry: 1 -> 10 snapshots (+9)
rows:          3 -> 12        (+9)
LOST COMMITS: 0
```

Most failures were requirement failures ("branch main has changed"), which catch a conflict before
anything is written to Glue. The ones that matter reached `UpdateTable`:

- PyIceberg side: `Cannot commit ... because Glue detected concurrent update to table version 7`.
- Gravitino side: the server log has **16** `CommitFailedException: Cannot commit
  custom.<db>.enrollments because Glue detected concurrent update`, each followed by
  `Retrying task after failure` (Iceberg's `Tasks` retry in the REST commit path).

CloudTrail shows that Gravitino's own `UpdateTable` calls, made with the container's `ASIA...`
session key, **carry `versionId`** and were rejected by Glue:

```
2026-09-18T21:00:03Z  ASIA…  UpdateTable  versionId=4  ConcurrentModificationException
2026-09-18T20:59:59Z  ASIA…  UpdateTable  versionId=3  ConcurrentModificationException
2026-09-18T20:59:59Z  ASIA…  UpdateTable  versionId=3  ConcurrentModificationException
2026-09-18T20:59:56Z  ASIA…  UpdateTable  versionId=2  ConcurrentModificationException
```

This was worth measuring rather than reading. `GlueTableOperations.persistGlueTable` sets
`versionId` through `DynMethods.builder("versionId").hiddenImpl("...UpdateTableRequest$Builder", ...)
.orNoop()` (Iceberg 1.11, `GlueTableOperations.java:81-87, 327-330`), which silently becomes a no-op
if the class does not resolve. Under Gravitino's isolated auxiliary-service class loader that was a
real possibility, and a no-op there would mean last-writer-wins against Glue. It resolves. The
guard is also skipped when a `lock-impl` is configured, so do not set one.

Direct Glue writes are invisible to Gravitino's audit log, which is expected. They show in
CloudTrail under the writer's own identity.

### Item 5: not measured

Needs an IAM role for Gravitino to assume, which this run could not create. Nothing about vending
is backend-specific in the source: the session policy is built from the loaded table's `location()`
and metadata location, which a Glue-backed table has like any other. Deployment-spec verification
item 2 (a vended `aws-irsa` credential, per-table scope, in QA) now covers it for the backend we
will actually run.

## What this settles

P5 is Option A. Gravitino becomes an authorizing, identity-attributing front for the Glue tables
that already exist. Nothing is migrated, and Airbyte, Dagster and dbt keep writing to Glue. A
Gravitino-path commit and a direct Glue commit on the same table cannot silently overwrite each
other.

The documented-production-backends objection in the deployment spec still stands: `custom` is not
on upstream's list. This spike is the evidence that it works for our case, not an upstream
guarantee, so pin the Gravitino version and re-run item 4 on every upgrade.

## Reproducing

Harness in the session scratchpad (`glab/`): `compose.yaml`, `gravitino.conf`, `kc/realm-ol.json`,
`sr/fe.conf`, `sr/setup.sql`, `seed.py`, `race.py`, and the `g` (Gravitino API) and `sq`
(StarRocks-as-user) wrappers. Not committed. The AWS resources were deleted after the run.
