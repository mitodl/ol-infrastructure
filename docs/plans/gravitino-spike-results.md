# SPIKE RESULT: Gravitino's group-claim role model

Task: `tk-spike-prove-gravitino-s-group-claim-role-model-o-59dc91`
Date: 2026-09-08
Verdict: **the claim under test PASSES.** One defect found. One item unverified.

## The claim under test

Gravitino maps a JWT claim to group membership, and a role granted to a **group** reaches a user
through it. If true, Keycloak roles drive catalog authorization with no per-user grant sync, which
is the property the project was otherwise going to license Cedar for
(`tk-decision-openfga-oss-second-role-vocabulary-vs-c-1497cb`).

Before this spike that was documentation only. It is now measured.

## Harness

Matches the two Lakekeeper spikes. All local containers.

| Component | Version |
|---|---|
| StarRocks | `4.1.3-8a8e186` (allin1-ubuntu, matches deployed) |
| Gravitino | `1.3.0` (`groupsFields` / `groupMapper` are new in this release) |
| Keycloak | `26.0.8` |
| MySQL client | `9.4.0` (for `authentication_openid_connect_client`) |
| Object store | MinIO |

Two end users, `alice` in Keycloak group `/ol_data_analyst`, `bob` in `/ol_data_engineer`, plus a
`gravitino-admin` service admin and an `ol-gravitino-machine` confidential client for the catalog
bootstrap credential.

Gravitino ran with the Iceberg REST service as an **auxiliary service** and the
**dynamic config provider**, which its docs require for access control:
"Standalone Iceberg REST server deployments do not support access control features."

## Results

| # | Item | Result |
|---|---|---|
| 1 | Per-user identity reaches the catalog through `security=JWT` | **PASS** |
| 2 | The `groups` claim arrives and is parsed | **PASS** |
| 3 | A role granted to a GROUP allows and denies correctly | **PASS** |
| 4 | Credential vending, per-table scope, revalidation | **PARTIAL** — vending proven, STS downscoping not |
| 5 | End-user principal in an audit trail | **PASS** |

### Item 1 — identity delegation through StarRocks: PASS

`alice` logged in to StarRocks over TLS with her own Keycloak id_token
(`authentication_jwt`, `authentication_openid_connect_client`), then queried the external catalog.
Gravitino's audit log for that session:

```
alice  LIST_SCHEMA  lakehouse.iceberg            SUCCESS  GRAVITINO_ICEBERG_REST_SERVER
       User-Agent=StarRocks-Iceberg-Connector/4.1.3-8a8e186
       X-Iceberg-Access-Delegation=vended-credentials
alice  LOAD_SCHEMA  lakehouse.iceberg.analytics  SUCCESS  GRAVITINO_ICEBERG_REST_SERVER
alice  LOAD_TABLE   lakehouse.iceberg.analytics.enrollments  SUCCESS  GRAVITINO_ICEBERG_REST_SERVER
```

The principal is `alice`, not the bootstrap machine account. StarRocks forwarded the end user's own
token, exactly as it does to Lakekeeper.

Two things confirmed in passing:

- **The bootstrap-credential requirement holds for Gravitino too.** `CREATE EXTERNAL CATALOG`
  succeeded with `security=JWT` paired with `iceberg.catalog.oauth2.credential` pointed at
  Keycloak's token endpoint. The machine principal needs no Gravitino grants, because `/v1/config`
  requires authentication but not authorization.
- **StarRocks sends `X-Iceberg-Access-Delegation: vended-credentials` on its own**, unprompted.

The catalog SQL that worked:

```sql
CREATE EXTERNAL CATALOG gravitino_cat PROPERTIES (
  'type'='iceberg',
  'iceberg.catalog.type'='rest',
  'iceberg.catalog.uri'='http://gravitino:9001/iceberg',
  'iceberg.catalog.warehouse'='iceberg',              -- the Gravitino CATALOG name
  'iceberg.catalog.security'='JWT',
  'iceberg.catalog.oauth2.server-uri'='http://keycloak:8080/realms/ol/protocol/openid-connect/token',
  'iceberg.catalog.oauth2.credential'='ol-gravitino-machine:machine-secret',
  'iceberg.catalog.oauth2.scope'='openid');
```

### Item 2 — the groups claim: PASS

Keycloak's group-membership mapper emits full paths, and the claim is present in **both** tokens:

```
alice  id_token      groups=["/ol_data_analyst"]   aud=["ol-starrocks-client","gravitino"]
alice  access_token  groups=["/ol_data_analyst"]   aud="gravitino"
```

The id_token is what StarRocks forwards, so `id.token.claim=true` on the mapper is load-bearing.
This is the same trap the project already recorded for `role_keys`.

`gravitino.authenticator.oauth.groupMapper.regex.pattern = ^/(.*)$` strips the leading slash, and
the task's prediction that this would be needed was correct.

**Control: membership comes from the token, not from Gravitino state.** Gravitino's stored group
object has no member list at all:

```json
{"name":"ol_data_analyst","audit":{...},"roles":["analyst_read"]}
```

Mutating **Keycloak only**, with no Gravitino change whatsoever:

```
STATE 0  alice groups=["/ol_data_analyst"]                      loadTable=200
         bob   groups=["/ol_data_engineer"]                     loadTable=403
  -- remove alice from the group, add bob to it, in Keycloak --
STATE 1  alice groups=null                                      loadTable=403
         bob   groups=["/ol_data_analyst","/ol_data_engineer"]  loadTable=200
  -- restore --
         alice groups=["/ol_data_analyst"]                      loadTable=200
         bob   groups=["/ol_data_engineer"]                     loadTable=403
```

Authorization follows the token. Throughout, `users/alice` and `users/bob` both reported
`roles: []`.

### Item 3 — group-granted role allows and denies: PASS

Role `analyst_read` = `USE_CATALOG` on the catalog + `USE_SCHEMA` on the schema + `SELECT_TABLE` on
the table. Granted **to the group `ol_data_analyst` only**. No user ever received a direct grant.

```
BASELINE (no grants)      alice listNamespaces=403  loadTable=403
                          bob   listNamespaces=403  loadTable=403
AFTER granting to GROUP   alice listNamespaces=200  loadTable=200
                          bob   listNamespaces=403  loadTable=403

bob's denial: ForbiddenException
  "User 'bob' is not authorized to load table 'lakehouse.iceberg.analytics.enrollments'"

users/alice roles: []      users/bob roles: []      groups/ol_data_analyst roles: ["analyst_read"]
```

Deny-by-default confirmed by the baseline. This is the claim the whole spike existed to test, and
it holds.

### Item 5 — audit trail: PASS, but off by default

`gravitino.audit.enabled` defaults to false; the server logs "Audit log is not enabled" at startup
and `gravitino_audit.log` stays empty. Once enabled, entries carry the end-user principal, the
operation, the fully-qualified object, outcome, source and client IP:

```
alice  LOAD_TABLE  lakehouse.iceberg.analytics.enrollments  SUCCESS  GRAVITINO_ICEBERG_REST_SERVER
bob    UNKNOWN     null                                     FAILURE  GRAVITINO_ICEBERG_REST_SERVER
       {http.method=GET, http.uri=/iceberg/v1/iceberg/namespaces/analytics/tables/enrollments,
        http.status=403}
```

Note the asymmetry: **denials log `UNKNOWN` as the operation and `null` as the object**, leaving
only the raw HTTP method and URI. Successes are fully structured, denials are not. Usable, but a
denial-rate alert would have to parse URIs.

### Item 4 — credential vending: PARTIAL

**The vending mechanism works, and authorization gates it.** `alice` loading an S3-backed table
with `X-Iceberg-Access-Delegation: vended-credentials` got a `storage-credentials` block carrying a
**per-table prefix**:

```json
"storage-credentials": [
  { "prefix": "s3://warehouse/wh/sales/orders",
    "config": { "s3.access-key-id": "minioadmin", "s3.secret-access-key": "minioadmin" } }
]
```

`bob`, who is not an analyst, got `403 ForbiddenException` and no credentials at all. So the
authorization decision gates credential issue, not just metadata.

**But read that credential carefully. It is the catalog's own static root key, verbatim.** Under
`credential-providers=s3-secret-key` the `prefix` is descriptive metadata only; the credential
handed to the user grants whatever the catalog's configured key grants, which here is the entire
MinIO instance. Anyone authorized for one table receives a credential good for the whole bucket.
**This is a trap: picking the convenient provider silently destroys the blast-radius property that
is the main reason to want vending at all.**

Real downscoping requires `credential-providers=s3-token` (STS AssumeRole). That path **could not
be verified here**: MinIO does not accept an AWS-style role ARN, and the load failed with

```
500  "Failed to generate credential using org.apache.gravitino.s3.credential.S3TokenGenerator"
```

That is an environment limitation, not a Gravitino defect. Whether `s3-token` produces genuinely
scoped, per-table, per-request-revalidated credentials the way Lakekeeper's signer was measured to
remains **unmeasured**, and needs real AWS STS to settle. Do not assume parity.

(The host disk was at 100% when this item was first attempted, and MinIO refused all writes with
`Status Code: 507`. Clearing 96G of `uv` cache unblocked it.)

## DEFECT: a cache-expiry miss is resolved as DENY

The first authorization check after a jcasbin cache entry expires returns **403 for a user who is
authorized**. The immediately following identical request returns 200.

Measured across four configurations, changing only `gravitino.authorization.jcasbin.cacheExpirationSecs`:

| TTL | idle gap | first call | next call | trials |
|---|---|---|---|---|
| 5s | 12s | **403** | 200 | 10/10 |
| 30s | 40s | **403** | 200 | 2/3 |
| 30s | 10s (< TTL) | 200 | 200 | 0/6 |
| 3600s (default) | 12s (< TTL) | 200 | 200 | 0/6 |

The behaviour appears and disappears with the TTL alone, so the cause is cache expiry, not the
particular value. It fails **closed**, so it is not a security hole. But at the default 3600s TTL
it means an authorized user who comes back after an idle hour gets one spurious "not authorized"
error before their query works. Through StarRocks that surfaces as a failed query, not a retryable
blip the user can't see.

I did not observe this at the literal 3600s setting, which would need an hour of idling. The
extrapolation from the 5s and 30s results is inference, not measurement.

Worth an upstream issue against Gravitino before anyone commits to it.

## What this does and does not settle

**Settles:** the role vocabulary question. Gravitino drives catalog authorization from Keycloak
group membership carried in the token. Per-user role assignment does not need syncing, which is the
recurring, drift-prone half of the OpenFGA option and the entire reason Cedar was attractive.

**Does not eliminate provisioning.** Two things still have to exist inside Gravitino:

1. **Users must be added to the metalake.** `alice` and `bob` were added explicitly. This is
   per-user, and it does not go away.
2. **Groups must exist in Gravitino, with role grants attached**, and the group name must match the
   post-`groupMapper` claim value.

The second set is O(number of governance roles), stable, and changes only when the role model
changes. The first is per-user but is a single idempotent "add user" with no privilege decisions in
it, unlike Lakekeeper's per-user grants or Polaris's per-user grants *plus* pre-created principals.

**Still unknown:** whether `s3-token` genuinely downscopes (item 4), and how the FE metadata cache interacts with this
(the cached-read bypass established for Lakekeeper is a StarRocks-side behaviour and applies here
too, so StarRocks GRANTs remain the query-time filter regardless).

## Non-negotiables from the prior spikes, re-checked

- **Fail-open check.** `gravitino.authenticators` defaults to `simple`, which reads an unvalidated
  HTTP Basic header. That is a genuine fail-open default. With `oauth` configured, unauthenticated
  requests to both `:8090` and the IRC on `:9001` returned **401**, so this deployment fails closed.
  Assert this explicitly in any real deployment, the way the Lakekeeper spike learned to assert
  `/management/v1/info`.
- **Grant at the narrowest level.** Privileges inherit downward, and `USE_CATALOG` + `USE_SCHEMA`
  are both required in addition to `SELECT_TABLE`.
- **Bootstrap credential at catalog init.** Required, as for Lakekeeper. StarRocks#75792/#75811
  apply to any REST catalog.
- **StarRocks GRANTs are still the query-time filter.** During item 1, `alice` was authorized by
  Gravitino but StarRocks refused the SELECT until its own grant existed
  (`ERROR 5203 ... you need the SELECT privilege on TABLE enrollments`). Defence in depth works as
  described; neither layer substitutes for the other.

## Reproducing

Harness lives in the session scratchpad (`gspike/`): `compose.yaml`, `gravitino.conf`,
`kc/realm-ol.json`, `sr/fe.conf`, and numbered `step*.sh` scripts, one per item. It is not
committed; it is a throwaway lab, and the versions above are what matter for reproducing it.
