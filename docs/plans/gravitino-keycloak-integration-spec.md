# Keycloak spec: identity for the Gravitino Iceberg REST catalog

Status: spec, ready for review
Date: 2026-09-08
Project: `wp-starrocks-iceberg-rest-catalog-jwt-identity-dele-13ccbe`
Task: `tk-spec-keycloak-service-account-and-audience-mappe-d7443a`
Supersedes the Lakekeeper framing of that task. Catalog choice: see
`docs/plans/iceberg-rest-catalog-evaluation.md` (recommendation: Apache Gravitino 1.3.0).

## Scope

Everything Keycloak has to emit, and everything Gravitino has to be told, for the end user's
Keycloak token forwarded by StarRocks under `iceberg.catalog.security=JWT` to authenticate and
carry role membership into the catalog.

Out of scope: the role-to-namespace grant model itself (that is
`tk-spec-cedar-authorization-policy-set-for-lakekeep-cf7568`, void as written and needing a
Gravitino rewrite), EKS placement, and the StarRocks-side catalog SQL.

## Decisions

| # | Decision |
|---|---|
| D1 | One dedicated audience value, `ol-gravitino-catalog`, emitted by an `AudienceProtocolMapper` on every client whose tokens reach the catalog. |
| D2 | New confidential service-account client, OIDC `client_id` `ol-gravitino-catalog`, for the catalog bootstrap credential; secret to Vault at `secret-operations/sso/gravitino-catalog`. Its client id doubles as the D1 audience value. |
| D3 | Per-client `access_token_lifespan` override on the two StarRocks clients. **Value needs sign-off**; recommended `3600`. Realm default stays 5m. |
| D4 | `principalFields = starrocks_username,preferred_username`. |
| D5 | Reuse the existing `role_keys` claim as Gravitino's group source. Do **not** create Keycloak groups. |
| D6 | `authority` set to the exact realm issuer per environment. It is the issuer check, and blank means no check. |
| D7 | `allowSkewSecs = 30`. |
| D8 | Assert the running authenticator at deploy time. Gravitino fails open. |

D5 is a change from the evaluation document, which assumed Keycloak groups and a
`^/(.*)$` group mapper to strip the leading slash from group paths. The realm already emits the
six governance roles as a flat `role_keys` array, so no groups and no slash-stripping are needed.

## Measured facts this rests on

Everything below was read from source or from live state, not from documentation.

**M1. The live Production realm issues 5-minute tokens.** From
`pulumi stack export --stack Production` of `ol-substructure-keycloak`, resource
`urn:pulumi:Production::ol-substructure-keycloak::keycloak:index/realm:Realm::ol-data-platform`,
read 2026-09-08:

```
accessTokenLifespan                 = 5m0s
accessTokenLifespanForImplicitFlow  = 15m0s
ssoSessionIdleTimeout               = 2h0m0s
ssoSessionMaxLifespan               = 24h0m0s
clientSessionIdleTimeout            = 0s
clientSessionMaxLifespan            = 0s
```

`src/ol_infrastructure/substructure/keycloak/ol_data_platform.py:99-100` sets only the two
session timeouts, so `accessTokenLifespan` is the Keycloak default and 5m is what is actually
live.

**M2. The ID token dies with the access token.** Keycloak 26.0.8,
`services/src/main/java/org/keycloak/protocol/oidc/TokenManager.java:1277`:

```java
idToken.exp(accessToken.getExp());
```

The MySQL `authentication_openid_connect_client` plugin sends the **ID** token, so M1 applies to
the token StarRocks stores and forwards.

**M3. A client-level lifespan override wins, capped by the client session.** Same file,
`getTokenExpiration`, lines 1063-1068:

```java
String clientLifespan = client.getAttribute(OIDCConfigAttributes.ACCESS_TOKEN_LIFESPAN);
if (clientLifespan != null && !clientLifespan.trim().isEmpty()) {
    tokenLifespan = Integer.parseInt(clientLifespan);
} else {
    tokenLifespan = realm.getAccessTokenLifespan();
}
```

and lines 1081-1085 clamp the result to `calculateClientSessionMaxLifespanTimestamp`, which with
`clientSessionMaxLifespan = 0` falls back to the realm's `ssoSessionMaxLifespan` of 24h. A
lifespan of `-1` (lines 1072-1076) means "expire with the SSO session", i.e. 24h here.

**M4. The audience mapper appends, and reaches the ID token.**
`services/src/main/java/org/keycloak/protocol/oidc/mappers/AudienceProtocolMapper.java:36`
declares `implements OIDCAccessTokenMapper, OIDCIDTokenMapper, TokenIntrospectionTokenMapper`,
and line 112 is `token.addAudience(audienceValue)`. Adding an audience does not replace
`idToken.audience(client.getClientId())` set at `TokenManager.java:1270`.

**M5. Gravitino validates `aud` by at-least-one match.** Gravitino v1.3.0,
`server-common/src/main/java/org/apache/gravitino/server/authentication/JwksTokenValidator.java:133-147`:

```java
// Audience validation per RFC 7519 (at-least-one match)
Set<String> acceptedAudiences = null;
if (StringUtils.isNotBlank(serviceAudience)) {
  acceptedAudiences = Collections.singleton(serviceAudience);
}
...
new DefaultJWTClaimsVerifier<>(acceptedAudiences, exactMatchClaims, null, null);
```

A multi-valued `aud` passes as long as it contains `serviceAudience`. Note the shape: Gravitino
accepts exactly **one** audience value, so every token reaching it must share that value.

**M6. StarRocks also checks `aud` by containment,** so adding a second audience does not break
the existing login path. `fe/fe-core/src/main/java/com/starrocks/authentication/OpenIdConnectVerifier.java`
on `branch-4.1` does `Arrays.stream(requiredAudience).anyMatch(claims.getAudience()::contains)`
against a `List<String>`.

**M7. `authority` is the issuer check.** `JwksTokenValidator.java:73`:
`this.expectedIssuer = config.get(OAuthConfig.AUTHORITY);`. `OAuthConfig.java:111-116` documents
`authority` only as a Web UI setting, but it is what the validator compares `iss` against, and
lines 141-144 skip the issuer check entirely when it is blank. This is a trap: the setting whose
documentation says "Web UI" is the one that decides whether a foreign issuer's token is accepted.

**M8. `principalFields` is an ordered fallback list, default `["sub"]`.**
`OAuthConfig.java:132-139`; `JwksTokenValidator.java:199-212` returns the first non-null claim.

**M9. `groupsFields` is new in 1.3.0 and silently ignores a non-List claim.**
`OAuthConfig.java:141-148` (default `["groups"]`, `VERSION_1_3_0`);
`JwksTokenValidator.java:216-232` returns the claim only `if (groupsObj instanceof List)`. A
single-valued Keycloak mapper emits a string and would be dropped with no error, so the source
mapper must be `multivalued=True`.

**M10. Gravitino fails open.** `core/src/main/java/org/apache/gravitino/Configs.java:233-246`,
`gravitino.authenticators` is `.createWithDefault(Lists.newArrayList("simple"))`, and `simple` is
an unvalidated HTTP Basic header.

**M11. `allowSkewSecs` defaults to 0** (`OAuthConfig.java:40-44`).

**M12. The Iceberg REST service shares the one authenticator.**
`iceberg/iceberg-rest-server/src/main/java/org/apache/gravitino/iceberg/server/GravitinoIcebergRESTServer.java:52`
calls `ServerAuthenticator.getInstance().initialize(serverConfig)`, and
`iceberg/.../RESTService.java:94-95` installs `IcebergAuthenticationFilter`, which extends the
shared `AuthenticationFilter`. One `gravitino.authenticator.oauth.*` block governs both the
management API and the catalog data path.

**M13. Neither StarRocks client has an audience mapper today.** The only
`AudienceProtocolMapper` in the realm is Superset's
(`ol_data_platform.py:358-367`).

**M14. `role_keys` is emitted for `ol-starrocks-client` only.** The
`UserClientRoleProtocolMapper` at `ol_data_platform.py:616-630` is a direct client mapper on
`ol-starrocks-client`. `ol-starrocks-cli` (`:675-700`) carries only the `starrocks_username`
attribute mapper (`:709-720`). Humans on the interactive PKCE path arrive through
`ol-starrocks-cli`, so **any role-driven catalog policy matches nothing for exactly those
tokens** until this is fixed.

**M15. A service account can carry `role_keys` too.** `ol_data_platform.py:202-220` grants
`ol_platform_admin` to the Superset service account via `ClientServiceAccountRole` precisely so
that its `client_credentials` token carries the claim. The same mechanism gives a machine
identity group membership in Gravitino without a per-machine grant in the catalog.

**M16. StarRocks' own principal mapping is already inconsistent, and this spec inherits it.**
`applications/starrocks/__main__.py:763-764` writes
`oauth2_principal_field = preferred_username` and `jwt_principal_field = preferred_username` into
fe.conf, while both security integrations set `"principal_field" = "starrocks_username"`
(`substructure/starrocks/__main__.py:649` and `:745`). fe.conf governs manually-created
`IDENTIFIED WITH authentication_jwt` users; the integration governs auto-provisioned ones. This is
the divergence already tracked by `tk-verify-first-then-fix-keycloak-group-sync-writes-c1bbf7`.

## D1: audience

`serviceAudience` is a single string (M5), so one value has to appear in every token that reaches
the catalog. Reusing a StarRocks client id cannot work: the interactive path issues through
`ol-starrocks-cli` and the JDBC path through `ol-starrocks-client`, and their default `aud` values
differ.

Create the audience as the machine client's own id (D2) and point every mapper at it with
`included_client_audience`, so the string has exactly one source of truth and cannot drift.

Clients needing the mapper:

| Client | Why |
|---|---|
| `ol-starrocks-client` | JDBC browser flow; FE exchanges the code and stores this ID token. |
| `ol-starrocks-cli` | Interactive PKCE (`starrocks-auth`); the token humans actually arrive with. |
| `ol-gravitino-catalog` | Catalog bootstrap `client_credentials` access token. |

Both `add_to_id_token=True` and `add_to_access_token=True` on all three. The ID token flag is the
load-bearing one for the two human paths (M2 sends the ID token); the access token flag is the
load-bearing one for the machine path, where `client_credentials` yields no ID token.

Safe against the existing StarRocks check by M6.

## D2: bootstrap machine client

`iceberg.catalog.security=JWT` cannot create the catalog on its own:
`RESTSessionCatalog.initialize()` runs with no user session, so `GET /v1/config` goes out
unauthenticated. The cure, already recorded in `substructure/starrocks/__main__.py:717-733`, is to
pair it with `iceberg.catalog.oauth2.credential`. That credential is a Keycloak
`client_credentials` pair and needs a client.

Model on `ol-marimo-app-client` (`ol_data_platform.py:782-808`): CONFIDENTIAL,
`service_accounts_enabled=True`, standard/implicit/direct-grants all off, secret written to Vault
by `vault.generic.Secret`, read back by the StarRocks substructure through
`vault_get_secret_output` as it already does for `secret-operations/sso/starrocks`
(`substructure/starrocks/__main__.py:609-623`).

The catalog bootstrap is the only machine identity this spec provisions. Gravitino has **no**
Kubernetes service-account authenticator (`gravitino.authenticators` accepts `simple`, `basic`,
`oauth`, `kerberos` only), so any pod that talks to the catalog directly needs its own Keycloak
service-account client. Whether Dagster and Airbyte do is not settled: see
`tk-decide-airbyte-lakekeeper-sequencing-does-airbyt-fe6119`. Provision those clients when that
question closes, using the same template plus a `ClientServiceAccountRole` for the role they need
(M15).

## D3: token lifetime — DECISION REQUIRED

This is the sharpest constraint in the design and the one item that needs a call rather than a
recommendation applied silently.

The chain: the live realm issues 5-minute tokens (M1); the ID token expires with them (M2);
StarRocks forces `TOKEN_REFRESH_ENABLED=false` in JWT mode, so the stored token is never
refreshed; and `OpenIdConnectVerifier` checks `exp` at **login only**, so the StarRocks session
outlives its own token. The result is a session that keeps answering local queries while every
catalog call fails, roughly 5 minutes after connecting.

Three options, all reachable through `keycloak.openid.Client(access_token_lifespan=...)`, which
maps to the client attribute in M3:

| Option | Effect | Cost |
|---|---|---|
| Leave at realm default | Catalog access breaks ~5 min into every session | Not viable |
| `"3600"` (recommended) | Catalog access works for 1h, then 401s until reconnect | A forwarded bearer token is valid for 1h if leaked |
| `"-1"` | Token expires with the SSO session, so 24h here (M3) | 24h leaked-token window |

Recommend `"3600"` on `ol-starrocks-client` and `ol-starrocks-cli` only, leaving the realm at 5m
for Superset, Marimo and everything else. It removes the failure mode from ordinary interactive
work while keeping the exposure window bounded. Long-running JDBC pools will still cross the
boundary, and the symptom to document is a mid-session catalog 401 cured by reconnecting.

Note that `ssoSessionIdleTimeout` of 2h does not protect a token already issued: an external
resource server validating a JWT against JWKS has no view of the Keycloak session, so a token
lives to its `exp` regardless. That is the whole of the exposure argument against `-1`.

## D4: principal field

```properties
gravitino.authenticator.oauth.principalFields = starrocks_username,preferred_username
```

`starrocks_username` first so a Touchstone human is the same name in a StarRocks `GRANT` and in a
Gravitino grant. `preferred_username` second so a machine token, which has no `saml_uid` attribute
and therefore no `starrocks_username` claim, resolves to `service-account-<client-id>` rather than
a UUID.

Two things to watch, both consequences of M8's silent fallback:

- A human whose `saml_uid` attribute is unset falls through to `preferred_username`, which in this
  realm is the email address (`registration_email_as_username=True`, `ol_data_platform.py:43`).
  The same person would then hold grants under two different names. Add a check that no Gravitino
  principal contains `@`.
- If `preferred_username` turns out to be absent from a `client_credentials` token in this realm's
  scope configuration, the principal falls through to nothing and the request is rejected with
  "No valid principal found in token" (`JwksTokenValidator.java:156-160`). Verify against a real
  machine token before relying on it; appending `sub` to the list is the fallback, at the cost of
  a UUID principal in grants and audit.

This does not resolve M16. StarRocks' own two paths still disagree with each other, and that stays
with `tk-verify-first-then-fix-keycloak-group-sync-writes-c1bbf7`. What this spec does is pick the
side that matches the auto-provisioned identity, which is the one real users get.

## D5: groups from `role_keys`

```properties
gravitino.authenticator.oauth.groupsFields = role_keys
gravitino.authenticator.oauth.groupMapper  = regex
gravitino.authenticator.oauth.groupMapper.regex.pattern = ^(.*)$
```

The realm already emits the six governance roles as a flat multivalued `role_keys` array (M14).
Gravitino's group source is a configurable claim name (M9), so it can read that array directly.
Gravitino "groups" then are `ol_platform_admin`, `ol_data_engineer`, `ol_data_analyst`,
`ol_researcher`, `ol_instructor`, `ol_business_analyst`, matching the ACL model in
`tk-design-per-user-acl-model-for-iceberg-rest-catal-ae239e` exactly. No Keycloak groups are
created, and the default identity regex is correct because role names carry no leading slash.

**Required fix: `ol-starrocks-cli` has no `role_keys` mapper (M14).** Add one, mirroring
`ol_data_platform.py:616-630` with `client_id` pointing at the CLI client and
`client_id_for_role_mappings="ol-starrocks-client"` so both paths project the same client's roles.
Without it, every human on the interactive path arrives with no groups and matches no policy.
This is worth doing regardless of catalog choice.

Do not add the shared `ol_roles` scope to either StarRocks client. The comment at
`ol_data_platform.py:632-638` explains why: that scope carries a second `role_keys` mapper for
`ol-superset-client` roles, and two mappers on one claim make Keycloak emit only one of them.

## D6/D7: issuer and skew

```properties
gravitino.authenticators                          = oauth
gravitino.authenticator.oauth.tokenValidatorClass = org.apache.gravitino.server.authentication.JwksTokenValidator
gravitino.authenticator.oauth.jwksUri             = https://sso.ol.mit.edu/realms/ol-data-platform/protocol/openid-connect/certs
gravitino.authenticator.oauth.authority           = https://sso.ol.mit.edu/realms/ol-data-platform
gravitino.authenticator.oauth.serviceAudience     = ol-gravitino-catalog
gravitino.authenticator.oauth.allowSkewSecs       = 30
```

Per environment the host is `sso.ol.mit.edu` (Production), `sso-qa.ol.mit.edu` (QA),
`sso-ci.ol.mit.edu` (CI), matching `derived_relying_party_id` at `ol_data_platform.py:25-28`.

`authority` is not optional. It looks like a Web UI setting and is in fact the issuer check (M7).
`allowSkewSecs` defaults to 0 (M11), which makes validation intolerant of ordinary clock drift
between the Keycloak pod and the Gravitino pod.

## D8: assert the posture at deploy time

`gravitino.authenticators` defaults to `simple`, an unvalidated HTTP Basic header (M10). A
misrendered config, a dropped key, or a chart upgrade that resets it produces a running, healthy,
completely unauthenticated catalog. The Lakekeeper evaluation found the same class of trap on the
other side, so this is not a reason to prefer either product, but it is a reason not to trust the
deployment to complain.

Minimum check, run post-deploy and on a schedule:

1. An unauthenticated `GET` against the Iceberg REST `/v1/config` endpoint returns 401.
2. A request bearing a token from a different realm returns 401 (proves D6 is in force).
3. Alert on either failing.

## Pulumi changes

All in `src/ol_infrastructure/substructure/keycloak/ol_data_platform.py`, inside the existing
`# STARROCKS [START]` / `[END]` block or a new `# GRAVITINO` block beside it.

```python
# GRAVITINO [START]
# Bootstrap credential for CREATE EXTERNAL CATALOG. security=JWT alone cannot
# create the catalog: RESTSessionCatalog.initialize() runs with no user session,
# so GET /v1/config goes out unauthenticated. This credential is used at catalog
# init only; every per-user operation carries the end user's own token.
ol_data_platform_gravitino_machine_client = keycloak.openid.Client(
    "ol-data-platform-gravitino-machine-client",
    name="ol-data-platform-gravitino-machine-client",
    realm_id=ol_data_platform_realm.id,
    client_id="ol-gravitino-catalog",
    client_secret=keycloak_realm_config.get(
        "ol-data-platform-gravitino-machine-client-secret"
    ),
    enabled=True,
    access_type="CONFIDENTIAL",
    standard_flow_enabled=False,
    implicit_flow_enabled=False,
    service_accounts_enabled=True,
    direct_access_grants_enabled=False,
    opts=resource_options.merge(ResourceOptions(delete_before_replace=True)),
)

vault.generic.Secret(
    "ol-data-platform-gravitino-machine-client-vault-credentials",
    path="secret-operations/sso/gravitino-catalog",
    data_json=Output.all(
        client_id=ol_data_platform_gravitino_machine_client.client_id,
        client_secret=ol_data_platform_gravitino_machine_client.client_secret,
        realm_id=ol_data_platform_gravitino_machine_client.realm_id,
        realm_name="ol-data-platform",
        url=ol_data_platform_gravitino_machine_client.realm_id.apply(
            lambda _: f"{keycloak_url}/realms/ol-data-platform"
        ),
    ).apply(json.dumps),
)

# Gravitino accepts exactly one serviceAudience, so every client whose tokens
# reach the catalog has to carry the same aud value. Both token flags matter:
# the two human paths forward the ID token, the bootstrap path has only an
# access token. Keycloak appends rather than replaces, and both StarRocks and
# Gravitino check aud by containment, so this does not disturb existing logins.
for _res_name, _client in (
    ("starrocks", ol_data_platform_starrocks_client),
    ("starrocks-cli", ol_data_platform_starrocks_cli_client),
    ("gravitino-machine", ol_data_platform_gravitino_machine_client),
):
    keycloak.openid.AudienceProtocolMapper(
        f"ol-data-platform-{_res_name}-gravitino-audience-mapper",
        realm_id=ol_data_platform_realm.id,
        client_id=_client.id,
        name="gravitino-audience",
        included_client_audience=(ol_data_platform_gravitino_machine_client.client_id),
        add_to_id_token=True,
        add_to_access_token=True,
        opts=resource_options,
    )

# The interactive PKCE path issues through ol-starrocks-cli, which had no
# role_keys mapper, so role-driven catalog policy matched nothing for exactly
# the tokens humans arrive with. Project the same ol-starrocks-client roles as
# the confidential client's mapper does.
keycloak.openid.UserClientRoleProtocolMapper(
    "ol-data-platform-starrocks-cli-role-keys-mapper",
    claim_name="role_keys",
    realm_id=ol_data_platform_realm.id,
    add_to_access_token=True,
    add_to_id_token=True,
    add_to_userinfo=True,
    claim_value_type="String",
    client_id=ol_data_platform_starrocks_cli_client.id,
    client_id_for_role_mappings="ol-starrocks-client",
    multivalued=True,
    name="starrocks-role-keys",
    opts=resource_options,
)
# GRAVITINO [END]
```

Plus, on both existing StarRocks client resources (D3, pending sign-off):

```python
access_token_lifespan = ("3600",)
```

`keycloak_url` is already a parameter of `create_ol_data_platform_realm`
(`ol_data_platform.py:12`), so the issuer string in the Vault payload needs no new input.

## Verification before this is called done

1. Decode a real ID token from `ol-starrocks-cli` and confirm `aud` contains both
   `ol-starrocks-cli` and `ol-gravitino-catalog`, and that `role_keys` is a JSON array (M9 drops
   a string silently).
2. Decode a real `client_credentials` access token from `ol-gravitino-catalog` and confirm
   `preferred_username` is present (D4's second risk) and `aud` contains the audience.
3. Confirm `exp - iat` on both is the configured lifespan, not 300.
4. Confirm an existing StarRocks login still succeeds after the audience mapper lands. M6 says it
   will; the audience change touches the one claim `oauth2_required_audience` reads.
5. Confirm the D8 checks fail closed against a token from another realm.

Items 1 through 3 need a Vault token this session did not have. They are cheap once someone with
one runs them and they should not be skipped: the whole chain is claims arriving in the right
shape.

## What this spec does not settle

- The grant model itself. `tk-spec-cedar-authorization-policy-set-for-lakekeep-cf7568` is void as
  written and needs re-filing against Gravitino roles and groups.
- Whether Dagster and Airbyte reach the catalog directly, and therefore whether they need their
  own service-account clients.
- M16, StarRocks' internal principal-field divergence.
- The FE metadata cache, which bypasses the catalog on repeated reads under any catalog. StarRocks
  `GRANT`s remain the query-time filter. Do not claim the catalog authorizes every access.
