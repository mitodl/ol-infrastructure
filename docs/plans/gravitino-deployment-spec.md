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
| P6 | `credential-providers=aws-irsa`, not `s3-token`. No static AWS keys anywhere. |
| P7 | In-cluster only. `ClusterIP` Service, no HTTPRoute, no NLB. |
| P8 | Official Helm chart `oci://registry-1.docker.io/apache/gravitino-helm`, version `1.3.11` (appVersion `1.3.0`). There is no operator. |
| P9 | QA 1 replica, Production 2 with a PodDisruptionBudget. `gravitino.cache.enabled=false` whenever replicas > 1. |
| P10 | Posture probe and grant reconciler run as CronJobs in the `gravitino` namespace. |

P5 and P6 change earlier project conclusions and need reading before the rest.

## P6: `aws-irsa`, and why the earlier "must be `s3-token`" does not carry over

The spike and the evaluation both say `credential-providers` must be `s3-token`, because
`s3-secret-key` vends the catalog's static root key. That comparison is right, but `s3-token` cannot
run on IRSA. `bundles/aws/.../S3TokenGenerator.java:90-95` builds its STS client from
`StaticCredentialsProvider.create(AwsBasicCredentials.create(accessKeyID, secretAccessKey))`, with
no fallback to the default credential chain, and `S3CredentialConfig.java:42-55` makes both keys
required. Using it would mean minting a long-lived IAM user key for a pod, which this repo does not
do.

`aws-irsa` is the provider built for EKS. `AwsIrsaCredentialGenerator.java:97-108, 344-376` calls
`AssumeRoleWithWebIdentity` with the pod's projected service-account token and the same per-table
session policy that `s3-token` attaches. Role ARN and token file fall back to the `AWS_ROLE_ARN` and
`AWS_WEB_IDENTITY_TOKEN_FILE` variables that IRSA injects (`:331-342`,
`webidentity/WebIdentityTokenSourceConfig.java:35`). The session is still named for the end user,
as `gravitino_irsa_session_<user>` (`:371`), so CloudTrail attribution survives.

Leave `s3-role-arn` unset. The generator then re-assumes the pod's own IRSA role with the session
policy applied, so there is one role, its trust policy is the one `OLEKSAuthBinding` already writes,
and no `sts:AssumeRole` permission is needed.

What the spike measured was `s3-token`. `aws-irsa` shares the session-policy code path but its
downscoping is not measured. It is the first thing to check in QA (see Verification).

Two consequences for the role:

- Gravitino's own catalog IO (metadata writes) does not go through the vending provider. Iceberg
  1.11's `AwsClientProperties` falls back to `DefaultCredentialsProvider` when no static key is set
  (`IcebergPropertiesUtils.java:52-59` only forwards keys that are present), so it picks up IRSA.
  That means the IRSA role itself needs direct S3 read/write on the warehouse, as the spike found.
- The vended credential is the intersection of that role and the session policy, so the role must
  be at least as broad as anything we want to vend.

`data_lake_query_engine_iam_policy_arn` from the `data_warehouse` stack already grants S3
Get/Put/Delete/List on every stage bucket plus Glue CRUD on the environment's databases
(`infrastructure/aws/data_warehouse/__main__.py:225-332`). Attach it, plus the
`data_lake_cross_environment_glue_denial_policy_arn` in non-production, exactly as
`applications/starrocks/__main__.py:278-319` does. Unlike StarRocks, attach only the policy for the
deployment's own environment (see P2).

`irsa_max_session_duration` must be at least `s3-token-expire-in-secs` (default 3600). The
`OLEKSAuthBinding` default is 3600, so the defaults agree. Raising the token lifetime means raising
both.

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
and a grant on a schema or table calls `loadSchema`/`loadTable` first (`MetadataObjectUtil.java:242-254`),
which triggers that import. So grants on pre-existing schemas should not need a bulk sync. This
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
restart_target_kind="Deployment")` as dagster and open_metadata do. Rotation restarts the pods,
which re-runs the substitution.

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
exercise this. It is a QA verification item. If it fails, the fix is on our side: add the IRSA role
ARN as a named principal in the key policy, or give the Gravitino role's session a KMS allowance
through a different provider configuration. Do not work around it with `s3-secret-key`.

## P7: exposure

`ClusterIP` Service `gravitino` exposing 8090 (management API) and 9001 (Iceberg REST, through the
chart's `extraExposePorts`, `values.yaml:509-513`). StarRocks' catalog URI becomes
`http://gravitino.gravitino.svc.cluster.local:9001/iceberg`.

No HTTPRoute and no internal NLB. Every consumer is in-cluster, and neither Concourse nor humans
need to reach it: grants are reconciled from inside the cluster (P10), and humans reach data through
StarRocks. That also keeps a management API that answers 403 with the name of the forbidden object
off the network.

NetworkPolicy is not enforced on the data cluster (`applications/clickhouse/__main__.py:1246-1249`),
so any pod in the cluster can reach both ports. Do not read "in-cluster only" as an authentication
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

The explanation sits above the marker because the Renovate regex requires the marker directly above
the assignment.

in `src/bridge/lib/versions.py`, deployed with `kubernetes.helm.v3.Release(chart="oci://registry-1.docker.io/apache/gravitino-helm", version=...)`
as `applications/toolhive_operator/__main__.py:77-93` does for its OCI chart. The chart's appVersion
is `1.3.0`, and Docker Hub has no `1.3.0` chart tag. Image `docker.io/apache/gravitino:1.3.0`.

Chart behaviour to override, all checked in `dev/charts/gravitino` at the tag:

- It never creates a ServiceAccount; it takes a name only (`values.yaml:536`). Create the IRSA SA
  through `OLEKSAuthBinding(create_irsa_service_account=True)` and pass its name.
- Default probes hit `/`. Use `/api/health/live` and `/api/health/ready`; `ready` checks the entity
  store (`docs/gravitino-server-config.md:321-334`).
- `docker-entrypoint.sh:65` adds `-XX:-UseContainerSupport`, so the JVM ignores the cgroup limit.
  Always set `GRAVITINO_MEM` explicitly. QA `-Xms1g -Xmx1g`, limit 2Gi. Production
  `-Xms4g -Xmx4g -XX:MaxMetaspaceSize=1g`, limit 6Gi, per the "moderate production" line in
  `docs/gravitino-server-config.md:376-385`.
- Leave `icebergRest.s3` unset. Setting it renders `s3-access-key-id` even when blank
  (`gravitino.conf:167-170`). In dynamic mode S3 settings belong on the catalog.
- The chart's Postgres init container only runs for its bundled subchart (`deployment.yaml:128-180`),
  hence the schema Job in P3.

Server configuration, through `additionalConfigItems` where the chart has no dedicated value:

```properties
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
is required.

`serviceAdmins` names a second Keycloak machine client, `ol-gravitino-admin`, used only by the
reconciler. It is separate from the bootstrap client in the Keycloak spec (D2) because a service
admin can create metalakes and grant anything, while the bootstrap credential is embedded in a
StarRocks catalog definition. The authorization spec covers what it needs.

Catalog properties (set by the reconciler, not the server config):

```properties
credential-providers = aws-irsa
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
Scrape 8090 only, with a ServiceMonitor.

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

Two CronJobs in the `gravitino` namespace, both on the `applications/omnigraph/council_probe.py`
pattern: a stdlib Python script in a ConfigMap on `python:3.12-slim`, credentials from
`OLVaultK8SSecret`, a non-zero exit as the signal. Failures reach `WorkloadJobFailedWarning` /
`WorkloadJobFailedCritical` (`infrastructure/grafana_alerting/metric_rules/eks_general.py:464, 490`),
and the job name goes into the staleness rule's regex (`:590-668`) so a job that stops running also
alerts.

- `gravitino-posture`, every 15 minutes. Unauthenticated `GET :9001/iceberg/v1/config` must return
  401, and so must `GET :8090/api/metalakes`. The rendered `gravitino.authenticator.oauth.authority`
  in the ConfigMap must equal the environment's issuer, and `gravitino.authorization.enable` must be
  `true`. This is D8 of the Keycloak spec, placed. No probe in this repo asserts a non-200 status
  today, so this is new.
- `gravitino-reconcile`, specified in the authorization spec.

Run posture once as a post-deploy Job too. A stack that deploys an unauthenticated catalog should
fail its `pulumi up`, not wait up to 15 minutes to be noticed.

## Verification before this is called done

1. The P5 spike, with each of its four unverified items answered. The spec's shape depends on it.
2. In QA, a vended `aws-irsa` credential for one table can read that table's objects, and gets 403
   on a sibling table's prefix and on `ListBucket` at the bucket root. That is the spike's `s3-token`
   result repeated for the provider we actually run.
3. The same credential can decrypt an SSE-KMS object in `ol-data-lake-<stage>-qa` (P4).
4. CloudTrail shows `gravitino_irsa_session_<user>` on those reads.
5. With 2 replicas in QA: grant on replica A, `loadTable` through replica B returns 200 immediately.
6. Posture job goes red when `gravitino.authenticators` is removed from a scratch deployment.

## What this does not settle

- Whether Dagster, Airbyte and notebooks talk to the catalog directly. Under Option A they do not
  have to, and `tk-decide-airbyte-lakekeeper-sequencing-does-airbyt-fe6119` mostly goes away. Under
  Option B they do and it stays open.
- Revocation latency. A vended credential lives to `s3-token-expire-in-secs` regardless of later
  revokes. 3600s is the default and the IRSA ceiling here. If an hour is too long, lower both.
- The StarRocks FE metadata cache still bypasses the catalog on repeated reads. StarRocks `GRANT`s
  remain the query-time filter.
