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

## Maturity

Polaris (1.7.0) and Gravitino (1.3.0) are both graduated ASF projects on monthly-ish release
cadences. Lakekeeper is pre-1.0 at v0.13.3 and is a single-vendor project with a commercial tier.
None of this is disqualifying on its own, but the governance difference is real: an ASF project
cannot move a feature we depend on behind a licence.

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
