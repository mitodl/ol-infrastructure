# Iceberg REST catalog evaluation: Lakekeeper vs Apache Polaris vs Apache Gravitino

Status: evaluation complete, recommendation pending one spike
Date: 2026-09-08
Project: `wp-starrocks-iceberg-rest-catalog-jwt-identity-dele-13ccbe`
Task: `tk-evaluate-iceberg-rest-catalog-options-lakekeeper-2e4ed9`

## Why this exists

The project's stated scope includes "evaluating Iceberg REST catalog options". That step was
skipped. Two spikes and every dependent spec assumed Lakekeeper. Before the Keycloak and EKS
specs encode Lakekeeper-specific configuration, and before the open p0 fork
(`tk-decision-openfga-oss-second-role-vocabulary-vs-c-1497cb`) is settled, the alternatives get
the same scrutiny.

Versions examined: Lakekeeper v0.13.1 (deployed pin; v0.13.3 released 2026-08-17), Apache Polaris
1.7.0 (released 2026-08-02, `main` at 1.8.0-SNAPSHOT), Apache Gravitino 1.3.0 (released
2026-06-29, `main` at 2.0.0-SNAPSHOT).

## Criteria

These come from what the two spikes proved actually matters, not from a generic feature grid.

| # | Criterion |
|---|---|
| A | Accepts an externally issued OIDC token as bearer. StarRocks forwards the Keycloak id_token verbatim under `iceberg.catalog.security=JWT`; the catalog must validate it against Keycloak's JWKS rather than minting its own tokens. |
| B | Catalog init: `GET /v1/config` is called with no user session. |
| C | Authorization vocabulary: can policy key on Keycloak roles/groups, or does it need a second vocabulary kept in sync? |
| D | Credential vending / per-table storage downscoping. |
| E | Audit trail carrying the end-user principal. |
| F | Open source vs enterprise gating. |
| G | Operational footprint in EKS. |

## Summary

| | Lakekeeper 0.13 | Polaris 1.7 | Gravitino 1.3 |
|---|---|---|---|
| A. External OIDC bearer | Yes, **measured** end to end | Yes, documented | Yes, documented |
| B. `/v1/config` | Requires auth; cured by bootstrap credential | Same StarRocks-side issue | Same StarRocks-side issue |
| C. Principal provisioning | **Self-registers on first touch** | **Must pre-exist; auth fails otherwise** | Must be added to metalake |
| C. Role vocabulary | Second vocabulary, own sync | Second vocabulary, own sync, roles only *select* existing grants | **Group claim drives role membership** |
| D. Credential vending | Yes, **measured** (per-table, identity + location revalidated) | Yes, optional principal in STS session name | Yes (S3, GCS, OSS, ADLS) |
| E. End-user audit | Yes, **measured** | Not verified | Not verified |
| F. Licensing | Apache-2.0 core, **Cedar authorizer is enterprise** | Apache-2.0, ASF top-level | Apache-2.0, ASF top-level |
| G. Footprint | Service + Postgres, plus OpenFGA + its own Postgres | Service + Postgres (+ OPA if used, beta) | Service + backend DB (+ Ranger if pushdown wanted) |

Bold entries are the ones that decide this.

## Findings

### Criterion A: external OIDC token

All three can validate a Keycloak-issued JWT against JWKS.

- **Lakekeeper.** `LAKEKEEPER__OPENID_PROVIDER_URI` / `OPENID_AUDIENCE` /
  `OPENID_SUBJECT_CLAIM` / `OPENID_ROLES_CLAIM` (`crates/lakekeeper/src/config.rs:395-436`).
  This is the only one of the three measured against our exact StarRocks build: the
  2026-08-07 spikes showed two distinct end-user subjects reaching Lakekeeper's audit log
  through StarRocks, and the follow-up spike showed per-user allow and deny actually enforced.
- **Polaris.** `polaris.authentication.type=external` delegates to Quarkus OIDC
  (`quarkus.oidc.auth-server-url`, `quarkus.oidc.client-id`), with
  `polaris.oidc.principal-mapper.{id,name}-claim-path` selecting the claims. There is a
  Keycloak integration guide in tree. Note the documented caveat that the default
  `PrincipalMapper` only handles JWT, not opaque tokens; that is fine for us.
- **Gravitino.** `gravitino.authenticators=oauth` with
  `gravitino.authenticator.oauth.jwksUri` and
  `tokenValidatorClass=org.apache.gravitino.server.authentication.JwksTokenValidator`.
  `gravitino.authenticator.oauth.principalFields` names the claim that becomes the username
  and defaults to `sub`.

Not a discriminator.

### Criterion B: `GET /v1/config` at catalog creation

This is a StarRocks-side problem, not a catalog-side one. `IcebergRESTCatalog`'s constructor
calls `RESTSessionCatalog.initialize()` with no user session, so the config call goes out
unauthenticated, and the fix (pair `security=JWT` with a bootstrap
`iceberg.catalog.oauth2.credential`) applies to any REST catalog that requires auth there.
StarRocks#75792 / #75811 apply equally to all three.

Not a discriminator.

### Criterion C: authorization vocabulary

This is the criterion that decides the evaluation, and it reverses part of the framing in the
open p0 decision task.

**Lakekeeper self-registers users on first contact.** `crates/lakekeeper/src/server/config.rs:185-235`
registers the authenticated principal on the `/config` call ("Register on first touch"),
stamping `UserLastUpdatedWith::ConfigCallCreation`. No per-user pre-provisioning. This is why
alice and bob appeared in the spike's audit log without setup.

What Lakekeeper does *not* give you is roles from the token. The spike measured
`/management/v1/role` staying empty with `OPENID_ROLES_CLAIM` set, so grants are explicit,
per-user or to Lakekeeper-managed Role entities. v0.13.1 does now carry token roles into
`RequestMetadata` (`service/authn.rs:465-467`, `request_metadata.rs:96-271`), but the only
consumer in tree is the admission layer (`service/admission.rs`), not OpenFGA tuples. SCIM is
explicitly not implemented: the concepts doc says "We are looking into SCIM support" and points
at issue #497. So the second-vocabulary-plus-own-sync cost in the decision task stands.

**Polaris is worse than Lakekeeper here, not better.** `DefaultAuthenticator` is the only
`Authenticator` implementation in the tree, and it carries this in its class javadoc
(`runtime/service/src/main/java/org/apache/polaris/service/auth/DefaultAuthenticator.java:59-61`):

> **This authenticator is used in both internal and external authentication scenarios. For now,
> it does not support federated principals that are not managed by Polaris.**

`resolvePrincipalEntity` loads the principal from the Polaris metastore by id or name and throws
`AuthenticationFailedException("Unable to authenticate")` when it is absent. And
`resolvePrincipalRoles` intersects the roles requested in the token against the grants the
principal *already holds in Polaris*: the OIDC role claim selects among existing grants, it does
not confer any. `PolarisAdminService` further refuses to create a federated principal, assign a
role to one, or rotate its credentials.

So Polaris under external OIDC needs every end user created as a Polaris PRINCIPAL entity *and*
every role granted to them inside Polaris. That is strictly more synchronisation than Lakekeeper,
which needs only the grants. The docs present the principal-roles-mapper as if Keycloak roles
drive Polaris authorization; the code shows they only filter it.

**Gravitino has the property the project wanted to buy Cedar for.** Two settings:

```properties
gravitino.authenticator.oauth.principalFields = preferred_username
gravitino.authenticator.oauth.groupsFields    = groups
```

`groupsFields` names the claim supplying group membership, and Gravitino's own documentation
states this "is how a role granted to a group reaches a user". Roles are granted to groups once;
membership arrives in the token. There is also a configurable `groupMapper` (regex by default)
for rewriting claim values into Gravitino group names, which matters because Keycloak emits group
paths as `/name`.

Users must still be added to a metalake before they can act in it, so per-user provisioning does
not disappear. What disappears is the per-user *role assignment* sync, which is the recurring,
drift-prone half. That is the same benefit Cedar was going to be licensed for, in an Apache-2.0
project.

### Criterion D: credential vending

All three vend subscoped storage credentials. Lakekeeper's is measured: per-table path with the
signer revalidating identity and location per request.

Polaris adds one thing worth noting: a feature flag that puts the principal name into the STS
session name (`polaris-<principal>` rather than `polaris`), which would make CloudTrail attribute
S3 access to the end user. Its own documentation notes the cost, that per-principal sessions
defeat credential caching.

### Criterion E: end-user audit

Measured only for Lakekeeper. Unverified for the other two. Do not assume it.

### Criterion F: licensing

Lakekeeper's Cedar authorizer is a Vakamo enterprise offering. `lakekeeper-bin/src/main.rs` at
v0.13.1 wires only OpenFGA and bails on any other external authorizer, despite the nightly docs
presenting Cedar as a standard option. This project has already lost time to that once.

Polaris and Gravitino are both ASF top-level projects under Apache-2.0 with their authorization
in the open-source build. Polaris additionally ships an OPA authorizer, currently marked Beta and
subject to breaking changes.

Lakekeeper is the only one of the three where the preferred authorization model sits behind a
commercial licence whose price we still do not know.

### Criterion G: operational footprint

- **Lakekeeper**, Option A of the open decision: Lakekeeper + Postgres, plus a separate OpenFGA
  service and its own Postgres. Two services, two databases.
- **Polaris**: one service plus its metastore. Add OPA only if the internal RBAC is not enough.
- **Gravitino**: one service plus its backend store. The Iceberg REST service is a component of
  it. Apache Ranger only if authorization pushdown to other engines is wanted, which is not our
  case.

Helm charts exist for all three.

## Unity Catalog: checked and disqualified

Added 2026-09-08 after it was raised. Unity Catalog OSS (unitycatalog/unitycatalog, v0.6.0
released 2026-08-20, LF AI & Data) **fails criterion A, and fails it harder than Polaris.**

It does implement the Iceberg REST API, including writes, so criterion B is fine:
`IcebergRestCatalogService` serves `/v1/config`, namespaces, and table create/update/delete. It
also has a `StorageCredentialVendor`. The problem is authentication.

`AuthDecorator.serve()` is installed as a global security decorator covering the Iceberg REST
service (`UnityCatalogServer.addSecurityDecorators`), and it contains:

```java
if (!issuer.equals(INTERNAL)) {
  throw new AuthorizationException(ErrorCode.PERMISSION_DENIED, "Invalid access token.");
}
```

with `Issuers.INTERNAL = "internal"`. Every request on the data path must carry a **UC-minted**
token. An external IdP token is accepted only at `POST /auth/tokens`, which exchanges it for a UC
token.

StarRocks under `iceberg.catalog.security=JWT` forwards the end user's Keycloak id_token verbatim
and performs no token exchange. So the delegation chain this whole project rests on cannot work
against Unity Catalog without upstream changes to StarRocks, to UC, or both. Polaris at least ships
an `external` mode that validates a foreign OIDC token directly and merely requires the principal to
pre-exist; UC has no equivalent for data-path requests.

Two lesser points, both already familiar:

- `AuthDecorator` resolves the principal with `userRepository.getUserByEmail(subject)` and rejects
  an unknown or disabled user, so per-user pre-provisioning in UC's local database is required, as
  in Polaris.
- Security decorators are installed only `if (serverProperties.isAuthorizationEnabled())`, carrying
  the comment `// TODO: eventually might want to make this secure-by-default.` So it fails open when
  unconfigured, the same class of trap as Gravitino's `simple` authenticator default.

Not worth a spike. It would re-enter consideration only if UC gained a direct external-issuer mode
on the data path.

## Maturity

Polaris (1.7.0) and Gravitino (1.3.0) are both graduated ASF projects on monthly-ish release
cadences. Lakekeeper is pre-1.0 at v0.13.3 and is a single-vendor project with a commercial tier.
None of this is disqualifying on its own, but the governance difference is real: an ASF project
cannot move a feature we depend on behind a licence.

## What the rest of the project says (swept 2026-09-08, after the Gravitino spike)

A pass over every task and memory in
`wp-starrocks-iceberg-rest-catalog-jwt-identity-dele-13ccbe`, looking for evidence that tilts the
choice. It does tilt, in both directions, and the net is a reframing rather than a winner.

### First, a correction to this document's own premise

An earlier section of this file said the project's "evaluate catalog options" step "was never
done". **That was wrong.** `tk-evaluate-iceberg-rest-catalog-options-for-jwt-de-17d9c3` closed
2026-06-25 having compared Nessie, Polaris, Lakekeeper and Unity Catalog OSS. The real gap was
narrower: it never considered Gravitino, and Gravitino is the one that changes the answer.

But that evaluation is not a reliable input any more, and this matters for how much weight the
incumbent deserves. It recommended "Lakekeeper v0.12.4 with Cedar" on the grounds that Lakekeeper
"maps `realm_access.roles` claim to per-table Cedar policies **without pre-registering
principals**" and was "the only catalog that has both first-class StarRocks validation AND direct
JWT-claim-driven RBAC". Since then:

- Cedar turned out to be **Vakamo enterprise-only**, absent from the OSS build.
- The roles claim **does not bind** in the OSS build; `/management/v1/role` stays empty.
- Gravitino **also** has direct claim-driven RBAC, now measured, not documented.
- The version pin (0.12.4) is two minors stale.

So Lakekeeper's incumbency rests on an evaluation whose central differentiator did not survive
contact with the software. That is not a reason to drop it, but it is a reason not to treat
"we already chose Lakekeeper" as evidence.

### Tilts toward Gravitino

**1. The ACL model this project already designed is purely role-to-namespace.** This is the
strongest single signal in the graph. `tk-design-per-user-acl-model-for-iceberg-rest-catal-ae239e`
(closed) maps the six governance roles onto namespace-level grants:

```
ol_data_analyst:     SELECT on silver_analytics, gold_analytics, gold_operations
ol_researcher:       SELECT on silver_analytics, gold_analytics
ol_instructor:       SELECT on gold_analytics, gold_operations
...
```

There is not a single per-user grant anywhere in it. That is precisely what Gravitino's group-claim
model expresses natively: six groups, six roles, done, with membership arriving in the token. Under
Lakekeeper OSS it is the awkward case, because the roles claim does not bind and each role has to
be projected onto every user as explicit grants. **The design that already exists fits Gravitino
and fights Lakekeeper.**

**2. The second-vocabulary cost is already on record as a known pain, next to a live bug of the
same shape.** `pf-openfga-operational-profile-measured-and-how-it--488b31` puts it plainly: grants
"must be projected from Keycloak into Lakekeeper by more automation — a second thing shaped exactly
like `keycloak_group_sync.py`, inheriting the same staleness and reload problems already open on
that one". Those problems are not hypothetical:
`tk-verify-first-then-fix-keycloak-group-sync-writes-c1bbf7` (p1, open) records that the existing
sync "will deny all OIDC login the moment the path is used", and
`tk-put-the-keycloak-starrocks-group-sync-on-a-sched-f660b3` records that a CronJob alone cannot fix
it because the FE never reloads. Committing to build a second sync of that shape, while the first
is broken, is a real strike.

**3. Smaller operational footprint.** The house pattern is one RDS instance per application
(`OLPostgresDBConfig`, `db.t4g.small`), and OpenFGA's vendor guidance says its datastore "should be
used exclusively for OpenFGA". Lakekeeper+OpenFGA is two services and two databases per
environment: **four RDS instances** across QA and Production. Gravitino is one service and one
database: **two**.

**4. End-user attribution in CloudTrail**, from the STS session naming measured in the spike. Not
required by any task, but the project's stated motivation is precisely to stop "sharing the pod's
IRSA service identity".

### Tilts toward Lakekeeper

**1. Kubernetes service-account authentication, which Gravitino does not have.** The closed
dual-catalog design (`tk-resolve-lakekeeper-access-for-native-auth-no-jwt-daf21e`) explicitly
depends on it: "enable `LAKEKEEPER__ENABLE_KUBERNETES_AUTHENTICATION=true` — pods authenticate via
their K8s SA token as `k8s~namespace~sa-name` principals, **no separate Keycloak service accounts
needed**". Lakekeeper's config confirms it (`enable_kubernetes_authentication`,
`kubernetes_authentication_audience`). **Gravitino 1.3.0 has no Kubernetes authenticator** —
`gravitino.authenticators` accepts only `simple`, `basic`, `oauth`, `kerberos` (verified against
the 1.3.0 docs).

Under Gravitino, the Dagster and Airbyte pods need Keycloak service-account clients and secrets in
Vault instead. This touches two already-planned implementation tasks. Two counterweights: the repo
provisions such clients routinely (`ol-marimo-app-client`, `ol-starrocks-client`, superset), so it
is more of an existing pattern rather than a new capability; and it keeps machine identities in
**one** vocabulary (Keycloak) instead of Lakekeeper's three (`oidc~sub`, `k8s~ns~sa`, and
Lakekeeper-managed roles).

**2. OpenFGA generalizes beyond the catalog; Gravitino's authorization does not.** This is the
strongest pro-Lakekeeper argument in the graph, and it is an argument about the estate rather than
about catalogs. `pf-openfga-candidate-use-cases-beyond-lakekeeper-an-654747` names two strong fits:
`ol-analytics-api`'s `require_org_manager`, which today answers "is this user a manager of this
org?" with a synchronous HTTP round-trip to MITx Online, and the unresolved MIT-admin membership
problem where "role DEFINITION is Pulumi-managed while role MEMBERSHIP has NO OWNING SYSTEM".
That memory's own verdict on Cedar — "an EMBEDDED authorizer ... NOT a standalone service other
applications can query" — applies verbatim to Gravitino's built-in authorization.

The counterweight is recorded in the same place: every extra consumer promotes OpenFGA from a
Lakekeeper dependency to a **tier-0 cross-service dependency** that fails closed with a ~10s hang
per request and a lagging `/health`.

**3. Gravitino ships a metadata-platform surface that overlaps OpenMetadata; Lakekeeper does not.**
This is real but narrower than it first looks — see the section below, which walks the actual
overlap against the live OpenMetadata instance. It is a scope-discipline risk, not a technical
blocker, and part of what I first counted here turned out to be symmetric between the two
candidates.

### Neutral

The Glue migration path (`iceberg-catalog-migrator --target-catalog-type REST`), dbt-starrocks
(unaffected, it reads through the StarRocks external catalog), the FE metadata-cache bypass
(StarRocks-side, applies to both), and Helm-via-Pulumi deployment (established for both).

### Gravitino vs OpenMetadata: the actual overlap

Measured against the live production OpenMetadata on 2026-09-08, because the first version of this
section asserted an overlap without checking one.

**What OpenMetadata actually holds today:**

| | count |
|---|---|
| database services | 2 (Glue, Starburst Galaxy) |
| tables | 3,465 (2,795 Trino, 670 Glue) |
| PII classification tags | 1,951 (1,384 `pii.nonsensitive`, 567 `pii.sensitive`) |
| data-quality test cases | 3,781, `testPlatforms: ["dbt"]` |
| glossary terms | 0 |
| owners / tiers / domains / certifications | 0 |

So its live value is discovery over ~3.5k tables, **PII classification at scale**, and **dbt test
results**. The governance features it also ships (glossary, ownership, tiering, domains) are
deployed but unused.

**Where the two genuinely overlap.** Gravitino is a metadata platform, not narrowly an Iceberg
catalog: alongside its catalogs (Hive, Glue, Iceberg, Paimon, Hudi, Delta/Lance, seven JDBC
flavours, Kafka, filesets, models) it ships `manage-tags-in-gravitino.md`,
`manage-policies-in-gravitino.md`, `manage-statistics-in-gravitino.md` and a `lineage/` subsystem
with server and Spark lineage. Four things are modelled by both: **a table inventory, tags,
lineage, and access policy.**

**Where they do not overlap at all.** OpenMetadata has PII auto-classification, data profiling,
data-quality test ingestion, a business glossary and the discovery UI humans actually use.
Gravitino has none of those. Note also that one of its apparent overlaps is a false positive:
Gravitino's `glossary.md` is a documentation glossary of terms ("API", "AWS"), **not** a business
glossary feature.

**The distinction that matters is role, not features.** OpenMetadata is a *passive* plane: it reads
metadata *about* systems and nothing queries data through it. Gravitino is an *active* plane:
engines resolve tables through it and it vends the credentials they use. A table would be known to
both, from opposite directions. That is duplication of a record, not of a function.

**One thing I initially miscounted as a Gravitino risk is symmetric.** OpenMetadata ingests today
from Glue and Trino. If any REST catalog becomes the catalog of record, the Glue source stops
seeing those tables — and that is equally true of Lakekeeper. The already-planned mitigation covers
both:
`tk-replace-the-five-trino-openmetadata-cronomjobs-m-0bbe3f` moves those jobs to **StarRocks and
Glue sources**, and a StarRocks source sees whatever StarRocks sees, external catalogs included. So
ingestion survives either choice. This is a project-wide dependency, not a differentiator. (Whether
OpenMetadata has a native connector for either catalog was not checked; the StarRocks path makes it
moot.)

**What is left as a genuine, Gravitino-specific risk** is narrow and is about discipline rather
than capability: Gravitino would ship tags, policies, statistics and lineage that we intend not to
use, and the drift risk is that someone starts using them and creates a split brain with
OpenMetadata. The mitigation is a one-line scoping decision — adopt Gravitino's Iceberg REST
service and its authorization, and declare OpenMetadata the governance and discovery plane.

**And one real gap that exists under either catalog, worth naming while it is cheap.** PII
classification lives in OpenMetadata (1,951 tables tagged) while access policy would live in the
catalog, and nothing links them. Those tags are decorative today — nothing enforces on them. The
moment anyone wants "deny access to `pii.sensitive`", they hit an unbridged gap. Gravitino's
policy/tag subsystem could in principle close it in one system, which is an argument *for*
Gravitino rather than against; Lakekeeper cannot, and OpenMetadata's own policy engine governs
OpenMetadata, not data access.

### Authorization head-to-head: Lakekeeper + OpenFGA vs Gravitino built-in

The two configurations actually on the table. Cedar is excluded (enterprise), Ranger is excluded
(wrong stack, and catalog-independent anyway). Both columns are backed by a spike against
StarRocks 4.1.3 and Keycloak 26.0.8; **bold** marks a measured result rather than a documented one.

| Dimension | Lakekeeper + OpenFGA | Gravitino built-in |
|---|---|---|
| Paradigm | ReBAC — relationship tuples in an external engine | RBAC + ownership, embedded (jcasbin) |
| Role membership source | **Explicit grants only.** The token's roles claim does not become a grantable principal | **The token.** Group claim drives role membership |
| Grantable principals | Users, Lakekeeper-managed Roles, **k8s service accounts** | Users, Groups |
| Role nesting | Yes, arbitrary | No — roles are flat sets of object+privilege |
| Ownership model | Project/warehouse admin roles | First-class on 13 object types, incl. **group ownership** |
| Object hierarchy | Project → Warehouse → Namespace → Table | Metalake → Catalog → Schema → Table |
| Column-level grants | No | No |
| Explicit DENY | **No** — positive assignments only | **Yes**, with hard precedence over ALLOW |
| Inheritance | **Downward; warehouse-level grants are very broad** | Downward; DENY cannot be undone by a lower ALLOW |
| Deny presentation | **404, hides existence** | **403, names operation and object** |
| List filtering | **Filtered to what you can see** | Not measured |
| Storage delegation | **Per-table remote signing; signer re-validates identity AND location per request** | **Per-table STS session policy; session named for the end user** |
| Credential revocation | Signer re-checks every request | **Cached until expiry** — a revoked grant does not invalidate an issued credential |
| Failure mode | **503 after a ~10s hang; `/health` lags** | In-process; no network hop |
| Misconfiguration hazard | **Unsupported authorizer silently becomes allow-all, zero log lines** | **`authenticators` defaults to `simple` (unvalidated Basic) — fails open** |
| Correctness defect found | None | **Cache-expiry miss resolves as DENY** — spurious 403 for an authorized user |
| Audit | **Per-user `oidc~<sub>` on each operation** | **End-user principal, operation, FQ object, outcome**; denials less structured |
| Reusable outside the catalog | **Yes** — OpenFGA is a standalone service | No — embedded, catalog-only |
| Footprint per env | 2 services, 2 databases | 1 service, 1 database |

#### The three differences that actually decide it

**1. Where role membership comes from.** This is the whole ballgame, because the ACL model this
project already designed is role-to-namespace with no per-user grants. Gravitino reads membership
from the token, so that model is six groups and six roles and nothing else moves. Lakekeeper OSS
cannot read it from the token, so the same model has to be projected onto every user by
synchronisation code — the second vocabulary. Everything else on this table is secondary to that.

**2. Gravitino can express DENY; Lakekeeper cannot.** Gravitino's DENY beats ALLOW regardless of
which role it came from or where in the hierarchy it sits, and its docs are explicit that "denials
cannot be circumvented by grants at lower levels". Lakekeeper's grant surface is positive
assignment only — OpenFGA's `but not` exclusion exists when *authoring* an authorization model, but
Lakekeeper owns that model, so an operator cannot write a deny rule. The practical consequence:
"analysts can read gold, except this one table" is one grant in Gravitino and a restructuring
exercise in Lakekeeper. Given that the measured Lakekeeper failure mode was *accidentally granting
too much* via a warehouse-level assignment, this is not academic.

**3. Lakekeeper's storage delegation is tighter, and revocation is the reason.** Both downscope per
table. But Lakekeeper's remote signer re-validates identity *and* the requested location on every
signing request — a signer URL is not a bearer of authority. Gravitino issues an STS credential and
caches it until expiry, so revoking a grant does not invalidate a credential already vended. If the
requirement is "revoke access and have it take effect now", Lakekeeper is materially better.
Gravitino's counter is that its STS session is named for the end user, so CloudTrail attributes S3
access to a human.

#### Two hazards that are not symmetric

Both fail open when misconfigured, but differently and with different odds of being caught.
Lakekeeper's is worse in character: setting an unsupported authorizer leaves a **running, healthy,
completely unauthorized catalog with no log line at all**, and only `/management/v1/info` reveals
it. Gravitino's is a default rather than a silent downgrade — leaving `gravitino.authenticators`
unset gives you `simple`, which trusts an unvalidated HTTP Basic header. Either way the deployment
must **assert** its own security posture at startup and alert on it; neither can be trusted to
complain.

Gravitino carries a live correctness defect that Lakekeeper does not: the first authorization check
after a cache entry expires denies an authorized user. It fails closed, so it is a usability and
correctness problem rather than a security one, but through StarRocks it surfaces as a failed query
and it should be filed upstream before adoption.

#### What is equal, and should not be used to argue either way

Neither supports column-level grants, so neither can enforce PII at column granularity without an
external layer. Both are bypassed by the StarRocks FE metadata cache on repeated reads, so
catalog-side authorization is not consulted on every user action under either, and StarRocks GRANTs
remain the query-time filter regardless. Both require the bootstrap catalog-init principal to be
provisioned explicitly. Both were proven to deliver per-user identity end to end through
`security=JWT`.

#### Not measured

For Lakekeeper: STS-based vending (the spike used remote signing against RustFS, which has no STS),
Cedar, and OpenFGA's `reconcile` maintenance path. For Gravitino: whether list operations are
*filtered* to visible objects or simply refused — Lakekeeper's filtering is proven and Gravitino's
is unknown, which matters for the discovery experience. For both: behaviour under real concurrency.

### The reframing

The project's own artifacts favour **Gravitino on the thing this project is actually for** —
per-role catalog authorization driven by Keycloak, matching an ACL model that is already designed
and contains no per-user grants — and favour **Lakekeeper on adjacent concerns**: machine
authentication for in-cluster pods, and OpenFGA as reusable estate infrastructure.

So the decision hinges on a question that is not about catalogs at all:

> **Does OL want a general-purpose, standalone authorization service?**

If yes, OpenFGA is worth its cost and Lakekeeper is the way in, with the second role vocabulary as
the price of admission. If no, Gravitino serves the designed model with less machinery, and the
k8s-auth gap is the price of admission instead.

That question should be answered by someone with a view of `ol-analytics-api` and the feedback
system, not by this project alone.

## Recommendation

**Do not switch on documentation alone, and do not settle the OpenFGA-vs-Cedar fork yet.**

Lakekeeper is the only candidate with measured end-to-end evidence against our exact StarRocks
build, covering identity delivery, enforcement, and storage delegation. That evidence is worth
real money and should not be discarded for a feature matrix.

But Gravitino's group-claim-driven role model dissolves the fork that is currently blocking the
project's top-priority decision. The whole reason Cedar was attractive was one role model driving
both StarRocks RBAC and catalog policy, and the whole reason it is a problem is that it costs
money and we could not exercise it. Gravitino appears to offer that property in an Apache-2.0
build. "Appears" is doing work in that sentence: it is documentation, not measurement, which is
exactly the epistemic position that the Cedar option is in and that the project has already been
burned by.

Concretely:

1. **Run a Gravitino spike on the same harness as the two Lakekeeper spikes** (StarRocks 4.1.3,
   Keycloak 26.x, local containers, two distinct end users). Prove or disprove, in order:
   per-user identity reaches the catalog through `security=JWT`; the `groups` claim actually
   drives role membership; a role granted to a group allows and denies correctly; credential
   vending works. Same bar the Lakekeeper enforcement spike had to clear. This is a bounded piece
   of work and it is the only thing that can responsibly settle the fork.
2. **Rank Polaris third and do not pursue it** unless the Gravitino spike fails and the Cedar
   price turns out to be unacceptable. It is dominated: more user-provisioning burden than
   Lakekeeper, and no group-claim path to role assignment. Its OIDC role mapping does not do what
   the docs imply.
3. **Hold the Keycloak and EKS specs** at the parts that are catalog-independent. The audience
   mapper, the machine service-account client, and the token-lifetime constraint apply to any of
   the three; the specific environment-variable names do not.
4. **Keep pricing discovery moving in parallel.** If Gravitino proves out, the Cedar quote stops
   mattering. If it does not, the fork is back and the quote is on its critical path.

## What is true regardless of the choice

Unchanged from the earlier spike findings, and worth restating because it survives every option
here:

- The FE metadata cache bypasses the catalog. Catalog-side authorization is not consulted on
  cached metadata reads, under any of these three. StarRocks GRANTs remain the query-time filter;
  the catalog is enforcement on actual catalog traffic. Do not promise that every access is
  authorized by the catalog or that the catalog audits every read.
- Per-table storage downscoping is a real blast-radius improvement over Glue's single ambient
  IRSA identity, and it is available in all three.
- TLS on the StarRocks FE is mandatory for every human client on the JWT path, independent of
  catalog choice.

## Sources

All file references are to the released versions named at the top, read from source rather than
documentation unless stated.

- Lakekeeper: `crates/lakekeeper/src/config.rs:395-436` (OIDC config),
  `crates/lakekeeper/src/server/config.rs:185-235` (first-touch user registration),
  `crates/lakekeeper/src/service/authn.rs:355-402` (authenticator construction, subject claim
  defaults `["oid","sub"]` with an explicit recommendation to set it in production),
  `docs/docs/concepts.md:127` (SCIM not implemented, issue #497).
- Polaris: `runtime/service/src/main/java/org/apache/polaris/service/auth/DefaultAuthenticator.java:59-61,
  120-160` (federated principals unsupported, principal must resolve, roles select among existing
  grants), `runtime/service/src/main/java/org/apache/polaris/service/admin/PolarisAdminService.java:1084,
  1559` (cannot create federated principal, cannot assign a role to one),
  `site/content/in-dev/unreleased/managing-security/external-idp/_index.md`,
  `site/content/in-dev/unreleased/configuration/config-sections/smallrye-polaris_authorization_opa.md`
  (OPA authorizer, Beta).
- Gravitino: `docs/security/access-control.md:505-530` (principalFields, groupsFields, users must
  be added to a metalake), `docs/security/how-to-authenticate.md:96-215` (JWKS validation, group
  mapping), `docs/security/credential-vending.md`, `docs/iceberg-rest-engine/starrocks.md`.
- StarRocks: `fe/fe-core/src/main/java/com/starrocks/authentication/OpenIdConnectVerifier.java`
  on `branch-4.1`.
