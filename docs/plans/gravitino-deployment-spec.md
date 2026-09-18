# Deployment spec: Gravitino Iceberg REST catalog on EKS

Status: spec, ready for review
Date: 2026-09-18
Project: `wp-starrocks-iceberg-rest-catalog-jwt-identity-dele-13ccbe`
Task: `tk-spec-lakekeeper-eks-placement-and-infrastructure-a0eb29`, rewritten against Gravitino.
Catalog choice: `docs/plans/iceberg-rest-catalog-evaluation.md`. Identity: `docs/plans/gravitino-keycloak-integration-spec.md`.
Grant model: `docs/plans/gravitino-authorization-spec.md`.

## Scope

Where Gravitino runs, what it stores its state in, how it reaches S3, how it is reached, and how it
is kept honest once deployed. The Keycloak side is already specified. The role and grant model is
in the companion authorization spec.

Gravitino facts below were read from the `v1.3.0` tag (commit `40fdf6ab`) of `apache/gravitino`.
Repo facts are from `main` at `e615f6ed4`.

## Decisions

| # | Decision |
|---|---|
| P1 | Run on the `data` EKS cluster, next to StarRocks. |
| P2 | Namespace `gravitino`, one Gravitino deployment per environment. QA and Production first; CI when something in CI needs it. |
| P3 | Dedicated RDS Postgres per environment, pinned to major version 16, with a Vault database mount at `postgres-gravitino`. |
| P4 | No new bucket. Tables stay where they are in `ol-data-lake-<stage>-<env>`. |
| P5 | Catalog backend is decided by a spike first: Iceberg `GlueCatalog` as a `CUSTOM` backend if it works, `JDBC` in the same RDS instance if it does not. |
| P6 | `credential-providers=aws-irsa`, not `s3-token`, vending from a dedicated S3-only role named in `s3-role-arn`. No static AWS keys anywhere. |
| P7 | In-cluster only. `ClusterIP` Service for the Iceberg REST port (9001) alone. The management API (8090) binds to `127.0.0.1` and is reachable only from inside the pod. |
| P8 | Official Helm chart `oci://registry-1.docker.io/apache/gravitino-helm`, version `1.3.11` (appVersion `1.3.0`). There is no operator. |
| P9 | QA 1 replica, Production 2 with a PodDisruptionBudget. `gravitino.cache.enabled=false` whenever replicas > 1. |
| P10 | Grant reconciler runs as a sidecar in the Gravitino pod. Posture probe runs as a CronJob in the `gravitino` namespace. |

P5 and P6 change earlier project conclusions and need reading before the rest. P6 and P7 together
close a hole that `aws-irsa` opens and `s3-token` does not.

## P6: `aws-irsa`, and why the earlier "must be `s3-token`" does not carry over

The spike and the evaluation both say `credential-providers` must be `s3-token`, because
`s3-secret-key` vends the catalog's static root key. That comparison is right, but `s3-token` cannot
run on IRSA. `bundles/aws/.../S3TokenGenerator.java:90-95` builds its STS client from
`StaticCredentialsProvider.create(AwsBasicCredentials.create(accessKeyID, secretAccessKey))`, with
no fallback to the default credential chain, and `S3CredentialConfig.java:42-55` makes both keys
required. Using it would mean minting a long-lived IAM user key for a pod, which this repo does not
do.

`aws-irsa` is the provider built for EKS. For a table request,
`AwsIrsaCredentialGenerator.java:97-108, 344-376` calls `AssumeRoleWithWebIdentity` with the pod's
projected service-account token and the same per-table session policy that `s3-token` attaches. The
token file falls back to `AWS_WEB_IDENTITY_TOKEN_FILE` (`FileWebIdentityTokenSource.java:43-58`) and
the role to `s3-role-arn`, then `AWS_ROLE_ARN` (`AwsIrsaCredentialGenerator.java:331-342`). The
session is still named for the end user, as `gravitino_irsa_session_<user>` (`:371`), so CloudTrail
attribution survives.

### The hole: `aws-irsa` hands out the whole role on a catalog-level request

For anything that is not a table path, `generate()` returns the role's credentials with no session
policy (`AwsIrsaCredentialGenerator.java:83-84`, "basic mode"). Upstream documents it as "credentials
with full permissions of the associated IAM role" (`docs/security/credential-vending.md:50`).
`s3-token` returns `null` in the same case (`S3TokenGenerator.java:72-75`), which is why the spike
never saw it.

One request reaches that path: `GET /api/metalakes/{m}/objects/catalog/{c}/credentials` on the
management port. `MetadataObjectCredentialOperations.java:61-129` serves it for `CATALOG` objects,
guarded only by `CAN_ACCESS_METADATA`, and `CredentialOperationDispatcher.getCredentialContexts`
builds a `CatalogCredentialContext` for a catalog identifier. So any user holding `USE_CATALOG`,
which is every read role in the authorization spec, can ask the management API for the vending
role's unscoped credentials. Had the vending role been the pod role carrying the query-engine
policy, that would be read, write and delete on every lake bucket, plus Glue and Bedrock. This is
read from source, not tested.

Two controls, both required:

1. The management API binds to loopback (P7). No pod but Gravitino's own can reach port 8090.
2. Vending uses a dedicated role through `s3-role-arn`, not the pod's role. It holds S3
   Get/Put/Delete/List on the `ol-data-lake-<stage>-<env>` buckets and nothing else. No Glue, no
   Bedrock, no `ListAllMyBuckets`. Its trust policy admits the data cluster's OIDC provider for
   `system:serviceaccount:gravitino:gravitino` only, since `AssumeRoleWithWebIdentity` authenticates
   with the pod's token rather than chaining from the pod role. If (1) ever regresses, the most a
   basic-mode credential can reach is lake object storage.

Neither control fixes the upstream behaviour. File it: basic mode should be opt-in, or refuse
catalog-level requests when authorization is on.

What the spike measured was `s3-token`. `aws-irsa` shares the session-policy code path for table
requests, but its downscoping is not measured. It is the first thing to check in QA (see
Verification).

### The pod role

Gravitino's own catalog IO (metadata writes) does not go through the vending provider. Iceberg 1.11's
`AwsClientProperties` falls back to `DefaultCredentialsProvider` when no static key is set
(`IcebergPropertiesUtils.java:52-59` only forwards keys that are present), so it uses the pod's IRSA
role. That role needs S3 read/write on the lake buckets, as the spike found, plus Glue on the
environment's databases under Option A only.

Do not reuse `data_lake_query_engine_iam_policy_arn`. Besides S3 and Glue it grants
`bedrock:InvokeModel`, `s3:ListAllMyBuckets` and `glue:TagResource` on `*`
(`infrastructure/aws/data_warehouse/__main__.py:225-305`), none of which Gravitino uses. Write a
dedicated policy in the Gravitino stack from the same helpers (`data_lake_glue_resources(env)` in
`lib/aws/iam_helper.py:325-392`), scoped to the deployment's own environment only (see P2), and
attach `data_lake_cross_environment_glue_denial_policy_arn` in non-production as
`applications/starrocks/__main__.py:305-319` does.

The vending role's `max_session_duration` must be at least `s3-token-expire-in-secs` (default 3600).
Raising the vended credential lifetime means raising both.

`gravitino_irsa_session_` is 23 characters and STS session names stop at 64, so a principal longer
than 41 characters makes the STS call fail. Kerberos short names are well under that. A Keycloak
service-account principal (`service-account-<client-id>`) can exceed it, so machine client ids that
reach the catalog must stay short.

## P5: catalog backend, and why this is a spike and not a decision

The dynamic config provider only serves catalogs whose provider is `lakehouse-iceberg`
(`DynamicIcebergConfigProvider.java:103-105`). Gravitino's separate `glue` catalog provider
(`catalogs/catalog-glue`) is therefore unreachable from the Iceberg REST service, and StarRocks
cannot use it. The `lakehouse-iceberg` backends are `HIVE`, `JDBC`, `MEMORY`, `REST` and `CUSTOM`
(`IcebergCatalogBackend.java:21-27`); there is no `GLUE`.

Two workable shapes:

Option A, `CUSTOM` backend with `catalog-backend-impl=org.apache.iceberg.aws.glue.GlueCatalog`.
`CUSTOM` loads the class through `CatalogUtil.loadCatalog` (`IcebergCatalogUtil.java:156-163`), and
`iceberg-aws` is a dependency of `iceberg-common`. If this works, Gravitino becomes an authorizing,
credential-vending front for the Glue tables that already exist. Nothing is migrated, and Dagster,
Airbyte and dbt keep writing to Glue directly. Writers going through Gravitino and writers going
straight to Glue then commit through the same `GlueCatalog` code, so they share one commit protocol
instead of the two-catalog coexistence problem recorded in
`pf-glue-to-lakekeeper-migration-iceberg-catalog-mig-d8aea7`. It also dissolves most of
`tk-decide-airbyte-lakekeeper-sequencing-does-airbyt-fe6119`: Airbyte never has to move.

Unverified for Option A: no Gravitino doc or test covers `GlueCatalog` as a custom backend; I have
not confirmed the Glue SDK classes are on the auxiliary service's classpath in the built image;
the docs say a custom catalog without `SupportsMetadataLocation` needs
`table-metadata-cache-impl=""` (`docs/lakehouse-iceberg-catalog.md:247`). I also have not
confirmed that Iceberg's `GlueCatalog` commit is conditional on the Glue table version, which is
what makes concurrent writers through two paths safe. All four are spike items.

Option B, `JDBC` backend. Iceberg's `JdbcCatalog` keeps table pointers in Postgres. Use a second
schema in the same RDS instance (P3). Existing tables move by `register-table` with
`iceberg-catalog-migrator`, no data copied. This is the Lakekeeper-era migration plan, and it
brings back the writer cutover: stop Glue writes, register, switch every writer.

Recommend Option A if the spike passes. Option B is the documented, supported path, but it costs a
migration and a cutover for every writer.

A side effect of Option A: existing Glue schemas and tables are imported into Gravitino's entity
store on first load (`SchemaOperationDispatcher.java:187-200`, `TableOperationDispatcher.java:138-151`),
and a grant on a schema or table first checks `schemaExists`/`tableExists`
(`MetadataObjectUtil.java:242-254`), whose default implementations call `loadSchema`/`loadTable`
(`SupportsSchemas.java:63-70`, `TableCatalog.java:84-90`) and so trigger that import. So grants on pre-existing schemas should not need a bulk sync. This
follows from the call chain; the spike should confirm it.

## P1, P2: cluster and namespace

Every consumer is on the `data` cluster: StarRocks, Dagster, Airbyte, JupyterHub (data), marimo and
ol-analytics-api all build their provider from `make_stack_reference(projects.EKS, "data.<env>")`.
Nothing that needs the catalog runs elsewhere.

Add `gravitino` to `eks:namespaces` in `infrastructure/aws/eks/Pulumi.data.QA.yaml` and
`Pulumi.data.Production.yaml`. The new application stack is `applications/gravitino`, with stacks
`applications.gravitino.QA` / `.Production` following the house project layout, and it checks the
namespace with `check_cluster_namespace`.

One Gravitino per environment, serving one catalog, `ol_data_lake_<env>`. StarRocks today registers
both the QA and the Production Glue catalogs in every instance so QA can query Production data.
That does not carry over to the per-user catalog. A QA Gravitino that vended Production credentials
would put Production access behind the QA deployment's weaker controls. QA StarRocks keeps its
existing Glue catalog for Production reads.

## P3: Postgres

Follow the standard sequence (`applications/superset/__main__.py:319-368`): security group for 5432
from `data_vpc["k8s_pod_subnet_cidrs"]` plus the Vault server SG, `OLPostgresDBConfig` on
`data_vpc["rds_subnet"]`, `OLAmazonDB`, then `OLVaultDatabaseBackend` with mount `postgres-gravitino`.
Put it in `data_vpc`, not `apps_vpc` as open_metadata does.

Sizing and settings:

- Engine: set `engine_major_version="16"`. The repo default is 18 (`components/aws/database.py:201-202`),
  and Gravitino documents Postgres 12 through 16 as tested and anything else as "not tested fully"
  (`docs/how-to-use-relational-backend-storage.md:98`). The entity store is the catalog's source of
  truth, so we should run a tested version and move up when Gravitino does.
- QA: `db.t4g.small`, single AZ. Production: `db.t4g.medium`, multi-AZ, no read replica. The
  Production stack default (`db.m7g.large` with a read replica, `lib/stack_defaults.py:9-65`) is
  sized for application databases. This one holds metadata pointers and grants.
- Schema: Gravitino creates its schema automatically only on H2 (`JDBCBackend.java:101`). For
  Postgres, `scripts/postgresql/schema-1.3.0-postgresql.sql` has to be applied before first start.
  It is all `CREATE TABLE IF NOT EXISTS`, so it can rerun. Run it from a Kubernetes Job on the
  Vault `app` credential under `SET ROLE gravitino` so tables are owned by the app role, which is
  the ownership the repo's `postgres_role_statements` revoke path expects (`lib/vault.py`).
- Upgrades are manual SQL (`upgrade-1.2.0-to-1.3.0-postgresql.sql` and so on) with Gravitino
  stopped (`docs/how-to-upgrade.md:17-30`). Renovate will offer chart bumps that are not safe to
  merge alone. The chart pin carries a comment saying so (P8), and each bump ships its upgrade script
  in the same PR.
- JDBC URL must carry the schema: `jdbc:postgresql://<host>:5432/gravitino?currentSchema=public`
  (`how-to-use-relational-backend-storage.md:164`).

### The password problem

The chart renders `entity.jdbcPassword` into a ConfigMap in plaintext
(`dev/charts/gravitino/resources/config/gravitino.conf:46-49`). Gravitino's config loader reads a
properties file only, with no environment substitution (`Config.java:86-106`), and file values
override `-D` system properties (`Config.java:69-71`). So neither a Secret reference nor `JAVA_OPTS`
can supply it.

What works: render a placeholder, inject the real value as an env var from the VSO-synced Secret
(`env` with `secretKeyRef`, `values.yaml:593-612`), and override `initScript` (`values.yaml:488-491`)
to substitute it into the config file before `exec`ing the entrypoint. Keep `SKIP_CONFIG_REWRITE=true`,
which the chart sets. The rewrite script resets `authorization.enable` and `catalog-backend` to
defaults (`dev/docker/gravitino/rewrite_gravitino_server_config.py:132-137`), which with this
config means authorization silently off.

Credentials come from `OLVaultK8SDynamicSecretConfig(mount="postgres-gravitino", path="creds/app",
restart_targets=[OLVaultRestartTarget(kind="Deployment", name=...)])`, the non-deprecated form
(`components/services/vault.py:633-637`, as `applications/omnigraph/__main__.py:942` uses it).
Rotation restarts the pods, which re-runs the substitution.

## P4: storage

No warehouse bucket. Under Option A the tables and their locations do not change. Under Option B,
`register-table` keeps each table's existing `metadata-location`. Either way the data stays in
`ol-data-lake-<stage>-<env>`, SSE-KMS with `alias/s3-analytical-data-<env>`.

That key is the open question here. Its policy grants encrypt and decrypt to `"Principal": {"AWS": "*"}`
conditioned on `kms:ViaService=s3.us-east-1.amazonaws.com` and `kms:CallerAccount`
(`infrastructure/aws/kms/__main__.py:88-110`). The session policy Gravitino attaches allows only
`s3:*` actions and no `kms:*` (grep of `bundles/aws/src/main/java` for `kms` finds nothing). If a
session policy limits what a wildcard-principal key policy grants, a vended credential can list and
fetch objects but cannot decrypt them. The spike ran against an unencrypted bucket and did not
exercise this. It is a QA verification item.

If it fails, naming the vending role's ARN in the key policy does not help: a resource policy that
names the role is still capped by the session policy, and only a policy naming the session ARN
escapes that cap, which is per user here. The realistic fix is upstream: have the session policy
include `kms:Decrypt` and `kms:GenerateDataKey` on the bucket's key, which is a small change to
`createSessionPolicy`. Carrying a patched image until that merges is the fallback. Do not work
around it with `s3-secret-key`, and do not move the lake buckets off SSE-KMS for it.

## P7: exposure

`ClusterIP` Service `gravitino` exposing 9001 (Iceberg REST, through the chart's `extraExposePorts`,
`values.yaml:509-513`) and nothing else. StarRocks' catalog URI becomes
`http://gravitino.gravitino.svc.cluster.local:9001/iceberg`.

The management API binds to loopback: chart value `webserver.host: 127.0.0.1`, rendered as
`gravitino.server.webserver.host` (`dev/charts/gravitino/resources/config/gravitino.conf:24`). This
is the first control in P6. It also keeps a management API that answers 403 with the name of the
forbidden object off the pod network. Consequences:

- Liveness and readiness probes use the Iceberg REST port, `/iceberg/health/live` and
  `/iceberg/health/ready` (`docs/iceberg-rest-service.md:674-689`), because the kubelet cannot
  reach loopback.
- Metrics are scraped from `9001/prometheus/metrics`, which serves the same registry as 8090.
- Anything that administers Gravitino runs inside the pod. That is the reconciler sidecar (P10).

No HTTPRoute and no internal NLB. Every consumer is in-cluster, and neither Concourse nor humans
need to reach it: grants are reconciled inside the pod, and humans reach data through StarRocks.

NetworkPolicy is not enforced on the data cluster (`applications/clickhouse/__main__.py:1246-1249`),
so any pod in the cluster can reach 9001. Do not read "in-cluster only" as an authentication
control. The authenticator is the control, and P10's probe checks it. Adding an in-cluster TLS hop
is out of scope here: bearer tokens cross the pod network in the clear, the same as every other
in-cluster HTTP service in this repo.

If a consumer outside the cluster appears later (e.g. a laptop PyIceberg client), add an
internal NLB following StarRocks' 9030 pattern (`applications/starrocks/__main__.py:1093-1185`).
Do not add a public route.

## P8: chart and image

```python
# Every chart bump needs its entity-store upgrade SQL applied first; see
# docs/plans/gravitino-deployment-spec.md (P3). Do not merge a bump alone.
# renovate: datasource=docker depName=gravitino-helm packageName=apache/gravitino-helm
GRAVITINO_CHART_VERSION = "1.3.11"
```

That pin goes in `src/bridge/lib/versions.py`, with the explanation above the marker because the
Renovate regex requires the marker directly above the assignment. Deploy it with `kubernetes.helm.v3.Release(chart="oci://registry-1.docker.io/apache/gravitino-helm", version=...)`
as `applications/toolhive_operator/__main__.py:77-93` does for its OCI chart. The chart's appVersion
is `1.3.0`, and Docker Hub has no `1.3.0` chart tag. Image `docker.io/apache/gravitino:1.3.0`.

Chart behaviour to override, all checked in `dev/charts/gravitino` at the tag:

- It never creates a ServiceAccount; it takes a name only (`values.yaml:536`). Create the IRSA SA
  through `OLEKSAuthBinding(create_irsa_service_account=True)` and pass its name.
- Default probes hit `/`. Use the Iceberg REST health endpoints (P7), since 8090 is on loopback.
- `docker-entrypoint.sh:65` adds `-XX:-UseContainerSupport`, so the JVM ignores the cgroup limit.
  Always set `GRAVITINO_MEM` explicitly, including `MaxMetaspaceSize`, because replacing the chart
  default drops its `-XX:MaxMetaspaceSize=512m` and metaspace is then unbounded. QA
  `-Xms1g -Xmx1g -XX:MaxMetaspaceSize=512m`, limit 2Gi. Production
  `-Xms4g -Xmx4g -XX:MaxMetaspaceSize=1g`, limit 6Gi, per the "moderate production" line in
  `docs/gravitino-server-config.md:376-385`.
- Leave `icebergRest.s3` unset. Setting it renders `s3-access-key-id` even when blank
  (`gravitino.conf:167-170`). In dynamic mode S3 settings belong on the catalog.
- The chart's Postgres init container only runs for its bundled subchart (`deployment.yaml:128-180`),
  hence the schema Job in P3.

Server configuration. These are the effective settings. Set each through the chart's dedicated
value where one exists (`auxService`, `icebergRest`, `authorization`, `audit`, `authenticators`,
`webserver`), and through `additionalConfigItems` only for keys the chart does not render. The chart
renders its own defaults for the dedicated ones (`authorization.enable: false`,
`authenticators: simple`), so pushing the same key through `additionalConfigItems` leaves it in the
file twice, and last-one-wins then decides whether the catalog is authenticated.

```properties
gravitino.server.webserver.host = 127.0.0.1
gravitino.auxService.names = iceberg-rest
gravitino.iceberg-rest.httpPort = 9001
gravitino.iceberg-rest.catalog-config-provider = dynamic-config-provider
gravitino.iceberg-rest.gravitino-metalake = ol_data_platform
gravitino.iceberg-rest.default-catalog-name = ol_data_lake_<env>

gravitino.authorization.enable = true
gravitino.authorization.serviceAdmins = service-account-ol-gravitino-admin

gravitino.audit.enabled = true
gravitino.audit.formatter.className = org.apache.gravitino.audit.JsonAuditFormatter
```

plus the `gravitino.authenticator.oauth.*` block from the Keycloak spec (D4 through D7). Standalone
Iceberg REST deployments support no access control at all, which is why the auxiliary-service form
is required. P10's posture check asserts that each security key occurs exactly once in the rendered
file.

`serviceAdmins` names a second Keycloak machine client, `ol-gravitino-admin`, used only by the
reconciler. It is separate from the bootstrap client in the Keycloak spec (D2) because a service
admin can create metalakes and grant anything, while the bootstrap credential is embedded in a
StarRocks catalog definition. The authorization spec covers what it needs.

Catalog properties (set by the reconciler, not the server config):

```properties
credential-providers = aws-irsa
s3-role-arn = <dedicated vending role, P6>
s3-region = us-east-1
s3-token-expire-in-secs = 3600
# Option A only:
catalog-backend = custom
catalog-backend-impl = org.apache.iceberg.aws.glue.GlueCatalog
table-metadata-cache-impl =
```

Audit goes to the `gravitino.audit` Log4j2 logger (`FileAuditWriter.java:36-71`; its file settings
are deprecated and ignored). The chart's log4j2 config has no audit appender and puts a console
appender on the root logger, so audit lines land on stdout mixed with server logs. Add a dedicated
console appender for `gravitino.audit` through `additionalLog4j2Properties` so Alloy ships a
separate, JSON-formatted stream to Loki. Remember that denials log `UNKNOWN` and a null object
(spike item 5), so a denial-rate alert has to match on HTTP status and URI.

Metrics: both ports serve `/prometheus/metrics` from one registry (`JettyServer.java:171-181`).
Scrape 9001, since 8090 is on loopback, with a ServiceMonitor.

## P9: replicas

Gravitino 1.3.0 supports several replicas against one database:

- Grants and role bindings are checked against `*_meta.updated_at` on every request
  (`JcasbinAuthorizer.java:93-99, 955-974`), so a grant or revoke on one replica holds everywhere on
  the next request.
- Catalog changes propagate through the `entity_change_log` table, polled every 3s
  (`EntityChangeLogPoller.java:126-196`). Ownership and name-to-id mappings are polled on the same
  interval (`JcasbinChangeListener.java`).
- The general entity cache (`gravitino.cache.*`, TTL 1h) has no change-log hook. After a
  drop-and-recreate of the same name on one replica, a peer can resolve the old id until the TTL
  lapses. This is inferred from the code (nothing under `core/.../cache/` references the change log),
  not observed. Set `gravitino.cache.enabled=false` when replicas > 1. The entity store is small,
  so the cost is some extra reads against Postgres.

QA 1, Production 2 with `maxUnavailable: 1`. Replica count as `gravitino:replicas` in stack config,
StarRocks-style.

The spike's cache-expiry defect (one spurious 403 after `jcasbin.cacheExpirationSecs` lapses) is
per-replica. Two replicas hit it twice as often, not less. It should be filed upstream before
Production, as the evaluation already says.

## P10: jobs

`gravitino-posture` is a CronJob in the `gravitino` namespace, every 15 minutes, on the
`applications/omnigraph/council_probe.py` pattern: a stdlib Python script in a ConfigMap on
`python:3.12-slim`, a non-zero exit as the signal. Failures reach `WorkloadJobFailedWarning` /
`WorkloadJobFailedCritical` (`infrastructure/grafana_alerting/metric_rules/eks_general.py:464, 490`),
and the job name goes into the staleness rule's regex (`:590-668`). That rule cannot see a CronJob
that has never succeeded (its KNOWN GAP comment), so a posture job that fails from its first run
alerts through `WorkloadJobFailed*` only. Checks:

1. Unauthenticated `GET :9001/iceberg/v1/config` returns 401.
2. A TCP connect to 8090 on each Gravitino pod IP is refused (P7's loopback binding held).
3. In the rendered `gravitino.conf` ConfigMap, `gravitino.authenticators`,
   `gravitino.authenticator.oauth.authority` and `gravitino.authorization.enable` each occur exactly
   once, with values `oauth`, the environment's issuer, and `true`.

This is D8 of the Keycloak spec, placed. No probe in this repo asserts a non-200 status today, so
this is new. Unlike `council_probe`, which deliberately runs without a ServiceAccount, this job needs
one, bound to a Role allowing `get` on that one ConfigMap (check 3) and `list` on pods in the
namespace (check 2). Nothing else.

Run posture once as a post-deploy Job too. A stack that deploys an unauthenticated catalog should
fail its `pulumi up`, not wait up to 15 minutes to be noticed.

The grant reconciler (authorization spec A7) runs as a sidecar container in the Gravitino pod,
because the management API it drives is on loopback. With two replicas there are two reconcilers.
Each run is idempotent and converges on the same desired state, so that is harmless. A sidecar has
no job status for `WorkloadJobFailed*` to see, so it exposes a last-success timestamp as a metric,
and an alert fires when that is older than an hour.

## Verification before this is called done

1. The P5 spike, with each of its four unverified items answered. The spec's shape depends on it.
2. In QA, a vended `aws-irsa` credential for one table can read that table's objects, and gets 403
   on a sibling table's prefix and on `ListBucket` at the bucket root. That is the spike's `s3-token`
   result repeated for the provider we actually run.
3. From another pod, a TCP connect to 8090 on the Gravitino pod IP is refused. From inside the pod,
   `GET /api/metalakes/ol_data_platform/objects/catalog/ol_data_lake_qa/credentials` with an
   analyst's token returns vending-role credentials, confirming the P6 hole is real and that the
   dedicated role is what it would expose.
4. The same credential can decrypt an SSE-KMS object in `ol-data-lake-<stage>-qa` (P4).
5. CloudTrail shows `gravitino_irsa_session_<user>` on those reads.
6. With 2 replicas in QA: grant on replica A, `loadTable` through replica B returns 200 immediately.
7. Posture job goes red when `gravitino.authenticators` is removed from a scratch deployment.

## What this does not settle

- Whether Dagster, Airbyte and notebooks talk to the catalog directly. Under Option A they do not
  have to, and `tk-decide-airbyte-lakekeeper-sequencing-does-airbyt-fe6119` mostly goes away. Under
  Option B they do and it stays open.
- Revocation latency. A vended credential lives to `s3-token-expire-in-secs` regardless of later
  revokes. 3600s is the default and the IRSA ceiling here. If an hour is too long, lower both.
- The StarRocks FE metadata cache still bypasses the catalog on repeated reads. StarRocks `GRANT`s
  remain the query-time filter.
