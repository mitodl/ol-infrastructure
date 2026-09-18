# Authorization spec: Gravitino roles, groups and grants for the data lake catalog

Status: spec, ready for review. Two decisions need an owner (A5, A6).
Date: 2026-09-18
Project: `wp-starrocks-iceberg-rest-catalog-jwt-identity-dele-13ccbe`
Task: `tk-spec-cedar-authorization-policy-set-for-lakekeep-cf7568`, rewritten against Gravitino.
Cedar is gone (Vakamo enterprise only), and so is Lakekeeper. See
`docs/plans/iceberg-rest-catalog-evaluation.md`.
Depends on: `docs/plans/gravitino-keycloak-integration-spec.md` (identity, D4 principal, D5 groups)
and `docs/plans/gravitino-deployment-spec.md` (topology, P10 jobs).

## Scope

Which Gravitino objects exist, which roles hold which privileges on them, how humans and machines
come to hold those roles, and how the whole thing is applied and kept from drifting.

## Decisions

| # | Decision |
|---|---|
| A1 | One metalake per environment, `ol_data_platform`, one catalog, `ol_data_lake_<env>`. Schemas are the existing `ol_warehouse_<env>_<layer>` Glue databases. |
| A2 | Six Gravitino groups and six roles, one of each per governance role, named identically to the `role_keys` values. Every privilege is granted to a role and every role to a group. No user holds a role directly. |
| A3 | Grants are enumerated per layer schema, never catalog-wide, except for the two engineering roles. |
| A4 | Desired state is one data structure in `ol_infrastructure`, consumed by both this catalog and the StarRocks `GRANT`s, so the two layers cannot disagree. |
| A5 | DECISION REQUIRED: what `ol_researcher` and `ol_instructor` may read. Recommended default: nothing, until a data owner says what. |
| A6 | DECISION REQUIRED: narrow the live StarRocks grants to the same layer map at the same time. Recommended: yes. |
| A7 | Applied by an in-cluster reconciler CronJob, as the service admin, through the REST API. There is no policy file to mount. |
| A8 | The reconciler adds users to the metalake from Keycloak role membership. It never grants anything to a user. |
| A9 | Schemas are owned by the `ol_data_engineer` group, not by whoever created them. |
| A10 | `DENY` is reserved for named exceptions. None on day one. |

## What exists to grant on

The June ACL model (`tk-design-per-user-acl-model-for-iceberg-rest-catal-ae239e`) grants on
`silver_analytics`, `gold_analytics`, `gold_operations` and `silver_operations`. None of those
exist. `aws glue get-databases` in account 610119931565 on 2026-09-18 shows one Glue database per
dbt layer per environment, created by `infrastructure/aws/data_warehouse/__main__.py:197-207`:

```
ol_warehouse_<env>_raw
ol_warehouse_<env>_staging
ol_warehouse_<env>_intermediate
ol_warehouse_<env>_dimensional
ol_warehouse_<env>_mart
ol_warehouse_<env>_reporting
ol_warehouse_<env>_integrations
ol_warehouse_<env>_external
ol_warehouse_<env>_migration      (dbt-created, no Pulumi resource)
```

The same Production Glue catalog also holds dozens of per-developer dbt schemas
(`ol_warehouse_production_<name>_<layer>`, e.g. `ol_warehouse_production_tmacey_mart`) with
production-derived data in them. They are the reason for A3. A catalog-wide `SELECT_TABLE` would
hand every developer's scratch copy to every analyst.

So the model below is the June model's intent (role to namespace, no per-user grants) re-expressed
against the real layers.

## A1: object layout

```
metalake  ol_data_platform                      owner: service-account-ol-gravitino-admin
  catalog ol_data_lake_<env>                    provider lakehouse-iceberg
    schema ol_warehouse_<env>_<layer>           owner: group ol_data_engineer (A9)
      table ...
```

The Iceberg REST `warehouse` is the Gravitino catalog name (`IcebergConfigOperations.java:101-108`
at v1.3.0), so StarRocks' human-facing catalog sets `'iceberg.catalog.warehouse'='ol_data_lake_<env>'`.

## A2, A3: roles and privileges

The role-to-layer intent already exists in one place: the `+grants` blocks of
`src/ol_dbt/dbt_project.yml` in ol-data-platform, written for Trino. This table is derived from
them, dropping the Trino-only roles (`read_only_production`, `reverse_etl`, `finance`, and so on).

| Layer | `ol_data_analyst` | `ol_business_analyst` | `ol_researcher` | `ol_instructor` |
|---|---|---|---|---|
| raw | | | | |
| staging | read | | | |
| migration | read | | | |
| intermediate | read | read | | |
| dimensional | read | read | | |
| mart | read | read | | |
| reporting | read | read | | |
| integrations | read | | | |
| external | | | | |

`ol_platform_admin` and `ol_data_engineer` get read and write on the whole catalog, dev schemas
included. The June model gave both "ALL on ALL namespaces", and data engineers need to reach their
own dev schemas.

`raw` and `external` get no analyst grant because dbt grants none on them. `raw` is Airbyte's
landing layer, not a dbt model layer.

In Gravitino terms, "read" on a layer is a role whose securable objects are:

```
catalog ol_data_lake_<env>                       USE_CATALOG
schema  ol_data_lake_<env>.ol_warehouse_<env>_X  USE_SCHEMA, SELECT_TABLE
```

`USE_CATALOG` and `USE_SCHEMA` are both required in addition to `SELECT_TABLE`. The spike measured
that `SELECT_TABLE` alone yields 403. `SELECT_TABLE` on the schema covers every table in it,
including ones created later, because privileges inherit downward.

The engineering roles:

```
catalog ol_data_lake_<env>   USE_CATALOG, USE_SCHEMA, CREATE_SCHEMA,
                             SELECT_TABLE, MODIFY_TABLE, CREATE_TABLE
```

Neither engineering role gets `MANAGE_GRANTS`, `MANAGE_USERS` or `MANAGE_GROUPS`. Grants change by
editing the desired state (A4), not by hand. A hand-made grant gets reverted on the next reconcile.

Groups are the six `role_keys` values, which Gravitino reads through
`groupsFields = role_keys` (Keycloak spec D5). Group objects in Gravitino hold no member list; the
spike showed membership comes entirely from the token, and a Keycloak-only change moved `alice`
from 200 to 403 with no Gravitino write.

## A4: one source of truth for both layers

StarRocks `GRANT`s stay the query-time filter under any catalog, because the FE metadata cache
serves repeated reads without calling the catalog. The spike showed the two layers intersect. Alice
was authorized in Gravitino and still refused by StarRocks until StarRocks granted her `SELECT` too.
So if the two disagree, the effective permission is the narrower one and nobody can tell which layer
did the refusing.

Express the table above once, as data, e.g. `ol_infrastructure/lib/data_lake_access.py`:

```python
class LakeAccess(str, Enum):
    read = "read"
    write = "write"

GOVERNANCE_LAYER_ACCESS: dict[str, dict[str, LakeAccess]] = {
    "ol_data_analyst": {
        "staging": LakeAccess.read,
        "migration": LakeAccess.read,
        ...
    },
    ...
}
CATALOG_WIDE_WRITE_ROLES = ("ol_platform_admin", "ol_data_engineer")
```

`substructure/starrocks/__main__.py` generates its per-database `GRANT SELECT ON ALL TABLES IN
DATABASE ...` from it. The Gravitino stack serializes it with `json.dumps` into the reconciler's
ConfigMap. Neither side templates the other's syntax.

## A6: the live StarRocks grants are wider than this

`substructure/starrocks/__main__.py` (the `_iceberg_roles_sql` block, lines 343-396) grants every
governance role, `ol_researcher` and `ol_instructor` included, `SELECT ON ALL TABLES IN ALL
DATABASES` on both `ol_data_lake_qa` and `ol_data_lake_production`, dev schemas included. The code
comment there defers database-level scoping "once the Glue catalog database naming is confirmed".
It is confirmed above.

This is a live over-grant only where `starrocks:enable_data_lake_integration` is true and a human
actually holds one of those roles. Neither was checked for this spec. Recommend landing A4's
StarRocks half before, and independently of, Gravitino. It is useful on its own and it removes the
disagreement before there is a second layer to disagree with.

## A5: researchers and instructors

No dbt grant names either role, so there is no recorded intent to derive from. The June model gave
`ol_researcher` "silver_analytics, gold_analytics" and `ol_instructor` "gold_analytics,
gold_operations", but those names map onto no real layer, and guessing that "gold" means `mart`
would hand learner-level data to anyone holding either role.

Recommend: both roles exist, both groups exist, neither holds any privilege until a data owner names
the layers or tables. A user who holds only one of these roles gets 403 on `listNamespaces`, which
is the correct failure for an undecided policy.

If the answer turns out to be "some tables in `mart`, not all", that is where table-level grants or
a schema-level grant plus a table-level `DENY` (A10) come in.

## Machine identities

| Principal | Needs | How |
|---|---|---|
| `ol-gravitino-catalog` (bootstrap, Keycloak spec D2) | Authenticate to `GET /v1/config` at StarRocks catalog creation | Nothing. The spike showed `/v1/config` requires authentication only. Not added to the metalake. |
| `ol-gravitino-admin` (reconciler) | Create the metalake, catalog, groups, roles, grants; add users | `gravitino.authorization.serviceAdmins`. It creates the metalake and so owns it. |
| Pipeline writers | Write any layer | Only under deployment Option B (they then talk to the catalog). A Keycloak service-account client per writer, holding the `ol_data_engineer` client role through `ClientServiceAccountRole` (Keycloak spec M15), so it arrives as group `ol_data_engineer`. Added to the metalake like a human. |

Under deployment Option A the pipeline writers keep writing to Glue directly, and there are no
pipeline principals in Gravitino at all.

`ol-gravitino-admin` is a new Keycloak client beyond what the Keycloak spec provisions: CONFIDENTIAL,
service accounts only, secret at `secret-operations/sso/gravitino-admin`. Its service account also
needs the `realm-management` client roles `view-users` and `view-clients` for A8. Its principal
resolves to `service-account-ol-gravitino-admin` through the D4 `preferred_username` fallback.
Verify that fallback against a real token first, as the Keycloak spec already asks.

## A7: how it is applied

Gravitino has no policy file. Everything above is REST API state in the entity store. It is reached
only from inside the cluster (deployment spec P7), so it cannot be applied from a Pulumi run on a
Concourse worker the way the StarRocks roles are.

`gravitino-reconcile` CronJob, every 15 minutes, on the deployment spec's P10 pattern. Each run:

1. Ensures the metalake and catalog exist, with the catalog properties from the deployment spec.
2. Ensures the six groups exist.
3. For each role, computes the desired securable objects and privileges from the ConfigMap, then
   grants what is missing and revokes what is present but not desired. It only touches the six
   roles it manages. Anything else is reported but left alone.
4. Ensures each role is granted to its group, and to nothing else.
5. Sets each existing `ol_warehouse_<env>_*` schema's owner to group `ol_data_engineer` (A9).
6. Runs A8.
7. Exits non-zero on any API error, so failures reach `WorkloadJobFailed*` and a stuck job trips the
   staleness rule.

Step 3 is what makes the ConfigMap the source of truth. Without the revoke half, a grant removed
from `GOVERNANCE_LAYER_ACCESS` would stay live forever.

A schema that does not exist yet (the dbt-created `migration`, or a new layer) is skipped with a
warning rather than failing the run. Under Option A, the grant call's `loadSchema` imports it from
Glue on first reference.

## A8: users

A user who has not been added to the metalake is denied everything, even when their groups hold
roles (`JcasbinAuthorizer.java:650-652`). Gravitino has no create-on-first-login option. So the
per-user step does not go away, but it carries no privilege decision.

The reconciler lists every user holding any of the six `ol-starrocks-client` client roles
(`GET /admin/realms/ol-data-platform/clients/{id}/roles/{role}/users`), computes each principal
exactly as D4 does (the `saml_uid` attribute if set, which is what `starrocks_username` is mapped
from, else `username`), and `POST`s any missing ones to `/api/metalakes/ol_data_platform/users`.

Properties that make this sync safe where `keycloak_group_sync.py` is not:

- It fails closed. A user it has not added yet is denied, not over-granted.
- It holds no privileges. Removing someone's Keycloak role revokes their catalog access on their next
  token, whether or not the reconciler has run.
- It does not remove users. A user who lost every role holds nothing through the token. Removing
  them from the metalake would also detach anything they own, so v1 reports such users and leaves
  removal to a person.

Same guard as D4: a principal containing `@` means `saml_uid` was missing and the same person may now
exist under two names. The reconciler refuses to add it and reports it.

## A9: ownership

Tables created through the Iceberg REST service are owned by their creator
(`IcebergTableHookDispatcher.java:67-97`), and an owner holds full control of the object regardless
of grants. Left alone, that means an engineer who creates a schema through StarRocks owns it
personally, and the ownership leaves with them.

The reconciler sets the owner of every managed schema to group `ol_data_engineer`, which Gravitino
supports. Table ownership stays with the creator. Engineers already hold write on the whole catalog,
so creator ownership of a table adds nothing they do not have, but for the other roles it would.
That is one more reason those roles get no `CREATE_TABLE`.

## A10: DENY

Gravitino supports `DENY` with precedence over `ALLOW` at any level. Nothing needs it today. It is
how a future "analysts read `mart` except these tables" is expressed without restructuring roles.
Add it to `GOVERNANCE_LAYER_ACCESS` as an explicit exception list, not as a hand-made grant.

One place it will be wanted: PII. OpenMetadata tags 567 tables `pii.sensitive`
(`docs/plans/iceberg-rest-catalog-evaluation.md`), and nothing enforces on those tags. A reconciler
step that reads those tags and emits table-level `DENY`s for roles without PII clearance would close
that. It is out of scope here and should be filed separately once someone decides who holds PII
clearance.

## Behaviour to expect, and to document for users

- A denied `listNamespaces` or `loadTable` is a 403 that names the object and echoes the policy
  expression. Gravitino does not hide the existence of what you cannot read.
- Inside a schema you can reach, listings are filtered to what you may read.
- The first request after `jcasbin.cacheExpirationSecs` elapses can 403 for an authorized user,
  and the retry succeeds (spike defect, to be filed upstream). Through StarRocks this is a failed
  query.
- Denials audit as `UNKNOWN` with a null object. Alert on status and URI, not on operation.
- Revoking a role does not invalidate an already-vended S3 credential before it expires
  (deployment spec, default 3600s).

## Verification before this is called done

1. In QA, a user holding only `ol_data_analyst` lists the seven analyst layers and not `raw`,
   `external` or any dev schema; `loadTable` on a `mart` table is 200 and on a `raw` table is 403.
2. A user holding only `ol_business_analyst` gets 403 on `staging`.
3. A user holding only `ol_researcher` gets 403 on `listNamespaces` (A5 default).
4. Removing a privilege from `GOVERNANCE_LAYER_ACCESS` and redeploying makes it disappear from
   Gravitino after the next reconcile.
5. A grant added by hand through the API is reverted by the next reconcile.
6. The same user's StarRocks `SHOW DATABASES` on the external catalog matches (1), which is what A4
   buys.
7. A new Keycloak user given `ol_data_analyst` is denied, then allowed after one reconcile, with no
   other change.

## What this does not settle

- A5 and A6, above.
- Column-level access. Gravitino has no column grants. Column-level PII would need an engine-side
  layer (StarRocks column masking), not the catalog.
- The FE metadata cache. Catalog authorization is not consulted on cached reads. StarRocks `GRANT`s
  are the query-time filter; this catalog is enforcement on catalog traffic and on storage
  credentials.
