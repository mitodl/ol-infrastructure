# Keycloak audit fixes and 26.8 upgrade: sequencing and design spec

Status: spec, ready for review
Date: 2026-10-05, revised 2026-10-06 with live realm reads and consumer checks (M12 to M15)
Project: `wp-keycloak-idiomatic-architecture-improvements-and-70b5e2`
Task: `tk-spec-sequencing-and-design-decisions-for-the-key-060ee7`

## Scope

The 2026-10-05 audit produced six epics and 33 open tasks. Each p1 and p2 task carries a dated
verification comment with corrected line numbers. This document does not repeat those. It
settles the order of work, the design of the three p1 changes in
`src/ol_infrastructure/substructure/keycloak` (passkey browser flow, realm client policy,
service-account scoping), and the gates on the 26.8 upgrade.

Out of scope: the app-side identity consolidation already tracked in
`wp-soa-login-flow-holistic-fix-up-apisix-keycloak-i-0189c1`, the X-Userinfo trust posture in
`wp-apisix-owned-oidc-trust-boundary-org-wide-securi-0409aa`, and the digital credentials (DCC)
defects, which are not Keycloak work (see D12).

## Decisions

| # | Decision |
|---|---|
| D1 | Order: passkey flow fix, then the pulumi-keycloak 6.14.0 bump on its own, then per-client hygiene with the client policy growing alongside it, then FGAP scoping in CI, then the 26.8 upgrade when the remaining blocker clears, then the realm restructure. Section 6 has the dependency reasons. |
| D2 | Fix the passkey browser flow in place: make the passkey subflow ALTERNATIVE so all top-level siblings are the same kind. Do not wait for the return to built-in flows. |
| D3 | Drop the organization subflow from the two staff-realm browser flows. It has never executed, and both realms hold organizations created outside Pulumi in every environment (M12), so letting it run would put an identity-first username form in front of users who have never seen one. |
| D4 | Keep the staff realms on a custom flow with no password step. Do not switch them to the built-in browser flow. **Needs sign-off**, this amends `tk-return-the-hand-copied-browser-and-first-broker--332e4f`. |
| D5 | One helper builds the passkey browser flow for both staff realms, keeping today's Pulumi resource names and aliases. |
| D6 | A client policy never matches a client that does not already conform, and every executor runs with `auto-configure` off. |
| D7 | PKCE is enforced through one policy whose condition is a `client-attributes` marker, set on a client in the same change that sets `pkce_code_challenge_method="S256"` on it. The policy can exist from the first conforming client and grows one client at a time. PKCE is never applied through `client-access-type` or `any-client`: both match clients that cannot carry the attribute (the built-in `account` client, `admin-cli`, and the two SAML clients, M12). `witan-cli` gets the marker only after the witan CLI sends PKCE on its device request, which it does not today (M13). |
| D8 | Direct access grants and implicit flow are turned off per client first, then locked by `reject-ropc-grant` and `reject-implicit-grant`. |
| D9 | `mitlearn-admin-client` is not scoped with FGAP. It loses its roles when mit-learn stops using the Admin API (`tk-mit-learn-get-is-sso-user-from-a-token-claim-and-0a9b85`). |
| D10 | `mitxonline-b2b-client` is split in two: a runtime membership client with an Organizations permission and Users `manage`, and a provisioning client that keeps realm-wide `manage-identity-providers`. The split removes `manage-realm` and the IdP roles from the per-request path. It does not remove realm-wide user management, which FGAP v2 cannot scope for this use. The trial runs on 26.7.4 in CI. **Needs sign-off**, the scoping task's done-when cannot be met this way. |
| D11 | The FGAP work is done under this project. `wp-enable-fine-grained-admin-permissions-fgap-on-sh-73d6d3` (discovery, no tasks) should be closed in favour of it. **Needs sign-off.** |
| D12 | The DCC task leaves this project's sequence. Its two mitxonline fixes (refuse download of a revoked credential, https signer URL) do not depend on anything here and should go first. **Needs an owner.** |
| D14 | `odl-video-app` keeps `view-users`, which its request and Celery paths use. `query-users` and `query-groups` are removed as redundant. `manage-users` stays until the OVS owners say whether `assign_group_users` is still needed; if it is, it moves to a separate service client so the login client holds no write role. This replaces "remove the roles when the moira migration is finished". |
| D13 | At the 26.8 upgrade, set the log category `org.keycloak.protocol.oidc.endpoints.TokenEndpoint.full-scope-allowed` to ERROR. Turning full scope off per client is restructure work and does not gate the upgrade. |

## Measured facts this rests on

M1 to M11 were read from source or from the repo at `origin/main` 3abd43920 on 2026-10-05.
Keycloak source was read at the 26.7.4 tag, the version pinned at `src/bridge/lib/versions.py:12`.
M12 to M15 were read on 2026-10-06: live realm state through the Admin API (GET only) in CI, QA
and Production, consumer code at each repo's default branch, and the provider releases.
Production reports 26.7.4. CI and QA report 26.7.5, which the pin does not explain; the
`ol-keycloak` image is built from a floating `26.7` base tag.

**M1. A flow level with both kinds drops its alternatives.**
`services/.../authentication/DefaultAuthenticationFlow.java`, `fillListsOfExecutions`: REQUIRED
and CONDITIONAL executions go in one list, ALTERNATIVE in another, and when both are non-empty
the alternative list is cleared with the WARN
`REQUIRED and ALTERNATIVE elements at same level!`. Conditional authenticators
(`conditional-user-configured`) are filtered out before the split. Production logged 16,697 of
these WARNs in the 7 days to 2026-10-05, all with the list
`[auth-cookie, identity-provider-redirector, null]` (see the comment on
`tk-passkey-browser-flow-mixes-required-and-alternat-6ec4ff`).

**M2. The staff-realm browser flows.** `ol_platform_engineering.py:1096-1182` and
`ol_data_platform.py:940-1026`: top level has `auth-cookie` (ALTERNATIVE, 10),
`identity-provider-redirector` (ALTERNATIVE, 20), an org subflow (ALTERNATIVE, 30) and the
passkey subflow (REQUIRED, 60) holding `auth-username-form` and
`webauthn-authenticator-passwordless`, both REQUIRED.

**M3. The redirector has no default provider in either staff realm.** The only
`ExecutionConfig` resources in `substructure/keycloak` are `ol_mit.py:214` (redirector,
`defaultProvider=touchstone-idp`), `ol_mit.py:293`, `ol_data_platform.py:1049` (first-login
flow) and two in `org_flows.py`. No `kc_idp_hint` is sent for a staff realm from anything in
`src/`. `ol-platform-engineering` declares no identity provider; `ol-data-platform` declares
Touchstone (`ol_data_platform.py:1091`).

**M4. The organization authenticator is a no-op without organizations.**
`OrganizationAuthenticator.authenticate` returns `attempted` when
`isEnabledAndOrganizationsPresent` is false. Both staff realms set
`organizations_enabled=True` (`ol_platform_engineering.py:45`, `ol_data_platform.py:105`) and
neither declares an organization in Pulumi; the only `Organization` resources are in
`olapps.py:887` and `org_sso_helpers.py:113`. With an organization present and no `login_hint`
the authenticator renders its own username form (`initialChallenge`). Both staff realms do
hold organizations that Pulumi did not create (M12), so the authenticator would not be a no-op
if the subflow ran.

**M5. Both staff realms set `sso_session_idle_timeout="2h"` and
`sso_session_max_lifespan="24h"`** (`ol_platform_engineering.py:98-99`,
`ol_data_platform.py:106-107`). These start to matter once the cookie authenticator runs.

**M6. Client policies run on Admin API writes.**
`services/.../resources/admin/ClientResource.java:159` triggers `AdminClientUpdateContext`
before applying an update and `ClientsResource.java:210` triggers `AdminClientRegisterContext`
on create. Pulumi writes clients through those endpoints.

**M7. Executor behaviour** (`services/.../clientpolicy/executor/`):

- `pkce-enforcer`: on REGISTER and UPDATE, `validate` throws `invalid_client_metadata` unless
  the client's PKCE method is `S256`, whatever flows the client has enabled. On
  AUTHORIZATION_REQUEST it requires `code_challenge` and `code_challenge_method`, and on
  TOKEN_REQUEST a `code_verifier`. One config key, `auto-configure`. Separately from the
  executor, a client with the PKCE attribute set must send PKCE on a device authorization
  request too: `DeviceEndpoint.java:147` calls `checkPKCEParams`.
- `reject-implicit-grant` and `reject-ropc-grant`: validate on REGISTER and UPDATE, and reject
  at the authorization request and the password-grant request respectively.
- `secure-redirect-uris-enforcer`: checks on REGISTER, UPDATE and both authorization-request
  events. Config keys: `allow-ipv4-loopback-address`, `allow-ipv6-loopback-address`,
  `allow-private-use-uri-scheme`, `allow-http-scheme`, `allow-wildcard-context-path`,
  `allow-permitted-domains`, `allow-open-redirect`, `oauth-2-0-compliant`,
  `oauth-2-1-compliant`.
- `full-scope-disabled` exists at 26.7.4 and acts on REGISTER and UPDATE only.
- Conditions available include `client-access-type`, `any-client` and `client-attributes`.

**M8. pulumi-keycloak 6.13.0 has what the design needs**, with one gap. Present:
`RealmClientPolicyProfile`, `RealmClientPolicyProfilePolicy`, `Realm.browser_flow`,
`Realm.first_broker_login_flow`, `Realm.admin_permissions_enabled`, and the FGAP v2 resources
`UsersAdminPermissions`, `ClientAdminPermissions`, `GroupAdminPermissions`,
`RoleAdminPermissions` (`UsersPermissions` is documented as v1 only). Absent: any resource for
an Organizations permission.

**M9. FGAP v2 at 26.7.4** (`server-spi-private/.../fgap/AdminPermissionsSchema.java:80-120`):
resource types Clients, Groups, Roles, Users and Organizations. Organizations has `manage` and
`view`. Users has `manage`, `view`, `impersonate`, `map-roles`, `manage-group-membership` and
`reset-password`. There is no per-attribute scope and no identity-provider type. This settles
the open question on the scoping task: Organizations is already an FGAP type in the version we
run.

**M10. Client inventory, as declared.** 32 `openid.Client` call sites in the four realm files,
plus one per contract in `learner_records.py:164` and one in `scim.py:66`. Public
clients: `ol-superset-cli`, `ol-starrocks-cli`, `toolhive-swe-cli`, `witan-cli`,
`witan-desktop`, `witan-ui`. PKCE `S256` is set on `toolhive-swe-cli`, `witan-desktop`,
`witan-ui` and `ol-grafana-client`. `witan-cli` has the standard flow off and no PKCE method.
Direct access grants are on for `ol-superset-client`, `ol-grafana-client` and `odl-video-app`;
implicit flow is on for `ol-open_metadata-client`. About a dozen clients do not set
`direct_access_grants_enabled` at all, and the provider field is Optional and Computed, so
their live value is whatever the server holds (M12 has the live values). The built-in clients
in each realm (`admin-cli`, `security-admin-console`, `account-console` and the rest) are not
in Pulumi and are matched by `client-access-type` and `any-client` conditions like any other.

**M11. Adding an organization member needs two permissions.**
`OrganizationMemberResource.addMember` calls `auth.orgs().requireManage(organization)` and
`auth.users().requireManage(user)` (lines 106 and 110 at 26.7.4). The user is not yet a member
when the second check runs, so that permission cannot be limited to the organization's members.
26.7.4 also has `manage-organizations`, `view-organizations` and `query-organizations`
realm-management roles.

**M12. Live realm state, 2026-10-06** (Admin API, all three environments unless noted).

- Flow bindings in Production match the code: `Organization browser` and
  `Organization first broker login` in olapps, `ol-browser-mit-flow`,
  `ol-browser-platform-engineering-flow` and `ol-browser-data-platform-flow` in the others.
  QA `ol-data-platform` has `firstBrokerLoginFlow` set to `sh first broker login` where
  Production has `first broker login`.
- Organizations in the staff realms: `ol-platform-engineering` has `Arbisoft` and `MIT`
  (plus `non-MIT` in CI); `ol-data-platform` has `MIT` (plus `gmail` in QA). None is in Pulumi.
- Staff users with a password credential: `ol-platform-engineering` has none in any
  environment (Production 38 users, 32 with a passkey). `ol-data-platform` has none in CI,
  2 of 14 in QA (neither has a passkey) and 14 of 568 in Production (11 of them with no
  passkey; 54 users there have a passkey).
- Direct access grants are on for exactly these clients: `ol-superset-client`,
  `ol-grafana-client`, `odl-video-app`, `admin-cli` in olapps, ol-mit and
  `ol-platform-engineering`, and the Starburst Galaxy SAML client in `ol-data-platform`. QA
  olapps also has a hand-made `ol-open-discussions-local` with direct grants and redirect `*`.
  Every client that omits the flag in Pulumi has it off. Implicit flow is on only for
  `ol-open_metadata-client`.
- `admin-cli` is not uniform. It is public in ol-mit. In the other three realms it has been
  made confidential with a service account, which matches access-forge using it with
  `client_credentials`.
- Public clients beyond the six in M10: the built-in `account`, `account-console` and
  `security-admin-console` in every realm, and two SAML clients (Sentry in
  `ol-platform-engineering`, Starburst Galaxy in `ol-data-platform`). `account` and the SAML
  clients have no PKCE attribute. `ClientAccessTypeCondition` tests only `publicClient` and
  `bearerOnly`, and neither it nor the executors look at the client protocol, so a
  `client-access-type` = `public` condition matches all of them.
- `ol-starrocks-cli` and `ol-superset-cli` already list their fixed loopback URIs next to the
  wildcard one. No realm has `adminPermissionsEnabled`.

**M13. Which consumers send PKCE.**

- `ol-starrocks-cli`: yes. `ol-data-platform` `bin/starrocks-auth` (e879a591) sends S256 and
  binds `http://localhost:18080/callback`, fixed.
- `ol-superset-cli`: yes. `InteractiveOAuthAuth` in `mitodl/superset-sup` (582de670) sends
  S256 and binds `http://localhost:8080/callback`. Neither CLI sends a `127.0.0.1` redirect.
- `witan-cli`: no. `agent-kit` `packages/witan-core/witan_core/remote/oidc.py:556-559` posts
  `client_id`, `scope` and an optional `audience` to the device endpoint, and the poll at
  `:608-615` has no `code_verifier` (671ad65f). Nothing in the repo builds a code challenge.
- APISIX `openid-connect`: no. One helper builds every plugin block
  (`components/services/apisix.py:718-727`) and does not set `use_pkce`, which exists at
  APISIX 3.19.0 with default `false` and makes lua-resty-openidc 1.9.0 send S256. One change
  there reaches Learn, MITx Online, learn-ai, analytics-api, the Learn JupyterHub, Airbyte,
  Dagster, Leek, Gwarek, Opik and the published Marimo apps. `ol-mitlearn-client` is shared
  by five stacks.
- Vault (vault-plugin-auth-jwt 0.26.4), Concourse (dex 1.14.0) and the data-platform
  JupyterHub (oauthenticator 17.4.0, client `ol-marimo-client`) already send S256.
- Superset does not; `"code_challenge_method": "S256"` in the authlib `client_kwargs` at
  `applications/superset/superset_config.py:67-82` turns it on.
- OpenMetadata 2.0.3 does not; `oidcConfiguration.disablePkce` defaults to true. It is
  configured as a confidential client on the code flow, not implicit. Its effective auth
  config lives in a database row that the Helm values only seed, and that row was not read.
- ocw-studio and OVS use social-core's `KeycloakOAuth2`, which is not a PKCE backend at any
  release through 6.0.0. They need a code change (a subclass with `BaseOAuth2PKCE`, or a move
  to `OpenIdConnectAuth`). open-discussions uses `OpenIdConnectAuth` at social-core 4.4.2,
  which gained PKCE at 4.8.7. social-core before 4.8.7 sends the method as lowercase `s256`,
  and `pkce-enforcer` compares with `S256` exactly.
- Grafana Cloud's SSO settings are not in the repo and the API read was refused.
  `ol-grafana-client` has S256 set live, which Keycloak enforces on its own, so a working
  Grafana login means Grafana sends PKCE. That is an inference.
- `ol-jupyterhub-client` and `ol-learn-ai-client` have no consumer in `src/`.

**M14. No code uses the password grant on the three clients that allow it.** Superset API
callers and OVS use `client_credentials`; OVS used a password grant for two days in April
2026, through `admin-cli` on `master`. The comments giving a reason
(`ol_data_platform.py:154`, `ol_mit.py:378`) describe uses that no longer exist, and
`ol-grafana-client` has none. Password-grant callers that do exist are operator scripts in
`scripts/` and local-dev, all through `admin-cli` and defaulting to the `master` realm. Not
covered: Grafana Cloud's own settings, and token-endpoint traffic, which was not sampled.

**M15. OVS and the roles on `odl-video-app`.** The moira code is gone from OVS
(`odl-video-service` PR 1464, issue 1002 closed 2026-04-28). OVS still calls the Admin API at
runtime with plain `requests`: group search, group members and user group lookups on request
and Celery paths, all satisfied by `view-users`. `manage-users` is used by the one-off
migration commands and by `assign_group_users`, which its docstring describes as an ongoing
tool. Infra still provisions the moira certificate (`k8s_secrets.py:197-198`), which nothing
reads.

## 1. Passkey browser flow (D2 to D5)

Target shape for both staff realms:

```
browser flow
  auth-cookie                              ALTERNATIVE  10
  identity-provider-redirector             ALTERNATIVE  20
  passkey subflow                          ALTERNATIVE  60
    auth-username-form                     REQUIRED     70
    webauthn-authenticator-passwordless    REQUIRED     80
```

The change per realm is one in-place update (the passkey subflow's `requirement`) and three
deletes (the org subflow and its two executions). The helper takes the realm and a name prefix
and must produce the existing resource names and aliases
(`ol-browser-platform-engineering-flow`, `ol-browser-data-platform-flow` and so on), because a
renamed flow is a replace and the `Bindings` resource points at the alias. It is a plain
function, not a `ComponentResource`, which would change every URN. `pulumi preview`
showing anything other than 1 update and 3 deletes per realm means the names drifted.

What changes for users:

- A live SSO session is honoured. Today every app login in these realms asks for username and
  passkey. After the fix a second app in the same browser signs in without a prompt, bounded by
  the 2h idle and 24h max in M5.
- The redirector runs. With no default provider (M3) it acts only on `kc_idp_hint`. In
  `ol-data-platform`, `kc_idp_hint=<touchstone alias>` will send the user straight to
  Touchstone. Today Touchstone is reachable only through its button on the username form.
- Nothing changes for the organization step: it did not run before and is removed.

Why not the built-in browser flow now (D4): the built-in forms subflow is username plus
password with passkeys offered alongside. Today the only ways in are a passkey, or in
`ol-data-platform` the Touchstone button. Moving to the built-in would make a password a
valid way in for anyone who has one. In Production that is nobody in
`ol-platform-engineering` and 14 users in `ol-data-platform`, 11 of them with no passkey
(M12). Beyond those, the built-in would start accepting a password the day one is set, by an
admin or through a reset-credentials flow, with nothing in Pulumi showing it. If sign-off
prefers the built-in, delete the existing password credentials first and remove the password
reset path from the realm.

Both realms hold organizations that are not in Pulumi (M12). D3 does not touch them; removing
the subflow only means they keep having no effect on login, as now. Who created them and
whether anything reads their membership was not determined, and is worth asking before they
are either declared in Pulumi or deleted.

Rollout: CI, QA, Production through the substructure pipeline. Verification per environment:

1. In the admin console the bound browser flow shows three ALTERNATIVE siblings.
2. Sign in to one app, open a second in the same browser, no prompt.
3. Production only: the Loki count of `REQUIRED and ALTERNATIVE elements at same level` for
   `{namespace="keycloak"}` falls to zero over the following 24h. Any remainder means another
   flow has the same shape.

The flow-binding reset (`tk-guard-against-realm-updates-silently-resetting-b-f2d0d5`) is
handled separately. Its fix is to set `browser_flow` and `first_broker_login_flow` on the
`Realm` resource (M8) so the binding does not depend on a refresh, after the CI reproduction
that task describes.

## 2. Client hygiene and the realm client policy (D6 to D8)

The policy is the guard against regression. It must never match a client that does not
conform, because once it does the next Pulumi update of that client is validated (M6, M7) and
fails the deploy. `auto-configure` would avoid the failure by rewriting
the client server-side during the update, and the rewritten field would then disagree with
Pulumi's inputs on the next refresh. That second half is an inference from M7, not something
observed; the rule (D6) costs nothing either way.

### 2.1 Per-client fixes

| Client | Change | State |
|---|---|---|
| `ol-superset-cli`, `ol-starrocks-cli` | `pkce_code_challenge_method="S256"`; drop `http://localhost:*/callback` and the unused `127.0.0.1` entry, keeping `http://localhost:8080/callback` and `http://localhost:18080/callback` respectively; explicit web origins | Ready. Both CLIs send S256 on those fixed ports (M13). Not covered: a user's own `~/.sup/config.yml` pointing another tool at `ol-superset-cli`. |
| `witan-cli` | `pkce_code_challenge_method="S256"` | Blocked on agent-kit. The CLI sends no PKCE on the device request or the poll (M13), and with the attribute set Keycloak requires both (M7). Add PKCE to `witan_core/remote/oidc.py`, release, and give users time to upgrade before setting the attribute. |
| `ol-vault-client`, `ol-concourse-client`, `ol-marimo-client` | `pkce_code_challenge_method="S256"` | Ready for Vault and Concourse, which send S256 unconditionally (M13). `ol-marimo-client` also serves the published Marimo apps through APISIX, so it waits for the gateway change. |
| `ol-open_metadata-client` | `implicit_flow_enabled=False` | OpenMetadata is configured for the code flow (M13), but its effective settings are a database row. Read `authenticationConfiguration` in CI before applying. |
| `ol-superset-client`, `ol-grafana-client`, `odl-video-app` | `direct_access_grants_enabled=False` | No code caller on any of them (M14). CI, then QA, then Production, watching the Keycloak event log for `invalid_grant` on these clients. Grafana Cloud's settings are the one thing not read. |
| `ol-mitlearn-client` | explicit web origins in place of `+` | The origins the Learn frontend calls from. |

Confidential clients behind a consumer that does not send PKCE yet:

| Consumer | Clients | What it takes |
|---|---|---|
| APISIX `openid-connect` | `ol-mitlearn-client`, `ol-analytics-api-client`, `ol-airbyte-client`, `ol-dagster-client`, `ol-leek-client`, `ol-gwarek-client`, `ol-opik-client`, `ol-marimo-client` | `use_pkce: True` in the shared helper's `base_oidc_config`. It reaches every route at once, so roll it through CI and QA as one gateway change, then set the attribute on each client after every stack using that client has redeployed. `ol-mitlearn-client` is used by five stacks. |
| Superset | `ol-superset-client` | `code_challenge_method` in `client_kwargs`. The authlib version in the image is unpinned and was not read. |
| OpenMetadata | `ol-open_metadata-client` | `disablePkce: false`, written to the database row by the existing `om_auth_config.py` Job, not only to the Helm values. |
| ocw-studio, OVS | `ocw-studio-app`, `odl-video-app` | App code change (M13). OVS at social-core 4.8.6 would also need `SOCIAL_AUTH_KEYCLOAK_PKCE_CODE_CHALLENGE_METHOD` set to `S256`, since that version defaults to lowercase. |
| open-discussions | `ol-open-discussions-client` | social-core 4.8.7 or later, then `SOCIAL_AUTH_OL_OIDC_USE_PKCE=True`. |
| Grafana Cloud | `ol-grafana-client` | Attribute already set. Confirm in the Grafana Cloud SSO settings that `use_pkce` is on. |

Clients with no interactive consumer (service accounts only, and `ol-jupyterhub-client` and
`ol-learn-ai-client`, which nothing uses) do not need the attribute and never get the marker.

### 2.2 Policy and profiles

Per realm, in a helper called once from each realm function:

- Profile `ol-pkce`: executor `pkce-enforcer`, `auto-configure: false`.
- Profile `ol-grants`: executors `reject-implicit-grant` and `reject-ropc-grant`,
  `auto-configure: false`.
- Profile `ol-redirects`: executor `secure-redirect-uris-enforcer`.
- Policy `ol-pkce-clients`: condition `client-attributes` on a marker (e.g.
  `ol.pkce=required`, set through the client's `extra_config`); profile `ol-pkce`.
- Policy `ol-managed-clients`: condition `client-attributes` on a second marker set on every
  OIDC client Pulumi declares; profiles `ol-grants` and `ol-redirects`.

The PKCE marker is set in the same change as the PKCE attribute, so the policy can be created
with the first conforming clients (the two data-platform CLIs, `toolhive-swe-cli`,
`witan-desktop`, `witan-ui`, `ol-grafana-client`, Vault, Concourse) and then follows section
2.1 client by client. From then on a client that carries the marker cannot lose PKCE through
a Pulumi edit or in the admin console.

Neither policy uses `client-access-type` or `any-client`. The earlier draft put PKCE on all
public clients and grants on all clients. The live reads (M12) show what those conditions
would also match: the built-in `account` client (public, no PKCE attribute), `admin-cli`
(direct grants on in three realms, and a different access type per realm), and two SAML
clients that Keycloak reports as public, one of them with the direct-grants flag on. A policy
matching any of those fails the next Admin API update of that client. The cost of markers is
that a client created by hand escapes both policies. Covering those is a later step: once the
built-in and SAML clients in a realm are either conforming or excluded, add an `any-client`
policy with `ol-grants`. Do that per realm, from a fresh client listing, not from this
document.

`ol-grants` on the managed clients has two preconditions, both now met except for the
clients in section 2.1: every declared client has direct grants and implicit flow off live
(M12), and the three direct-grant clients have no caller (M14). Set the two flags explicitly
in Pulumi on the clients that omit them, in the same change that adds the marker, so the
input and the server agree.

Password grants through `admin-cli` are not affected while the policy is marker-based. If an
`any-client` grants policy is added later, the operator scripts in `scripts/` that default to
`master` keep working, and passing `--auth-realm` for an app realm stops working. The SCIM
admin UI setting `spi-realm-restapi-extension-scim-accept-admin-cli-login`
(`applications/keycloak/__main__.py:532-540`) exists for an `admin-cli` password login and
would need the documented `client_credentials` alternative first.

The `secure-redirect-uris-enforcer` config is not settled here. Most redirect URIs end in `/*`
and the CLIs and Vault use `http` on loopback, so the starting point is
`allow-wildcard-context-path`, `allow-ipv4-loopback-address` and `allow-http-scheme` on, and
`oauth-2-1-compliant` off (it rejects `localhost` by name). A client with the standard flow on
and no redirect URIs fails validation outright. Settle the values by applying the profile to
the CI realms and reading which client updates fail, then tighten wildcards as a later pass.

### 2.3 Full scope

`full_scope_allowed` defaults to true in the provider and is set nowhere. Turning it off needs
explicit role scope mappings per client and belongs with the client factory
(`tk-extract-a-keycloak-client-factory-shared-client--5ad1b8`), where the default can flip in
one place. `full-scope-disabled` is added at the end of that work, under a condition that
excludes the built-in clients, as the 26.8 upgrade notes advise.

## 3. Service-account scoping (D9 to D11)

FGAP v2 can scope by resource, not by field (M9). That decides each client differently.

`mitlearn-admin-client` reads federated identities and writes one attribute, `emailOptIn`. The
narrowest FGAP permission that allows the write is Users `manage`, which is what it has now.
The fix is to remove the need: `is_sso_user` from a token claim and `emailOptIn` out of the
Admin API round trip (task `0a9b85`; `unsubscribe()` is a second caller). The client and its
three roles are deleted when that ships.

`mitxonline-b2b-client` holds seven realm-management roles and is used on enrollment-code
requests. Split it:

- Runtime membership client: an Organizations `manage` permission on the B2B organizations,
  and Users `manage`. Adding a member requires both (M11), and the second cannot be limited
  to an organization, so this client keeps the equivalent of `manage-users`. It loses
  `manage-realm`, `view-realm` and both IdP roles.
- Provisioning client: `manage-identity-providers`, `view-identity-providers` and
  `manage-organizations` in place of `manage-realm`. Identity providers have no FGAP type
  (M9), so this stays realm-wide. The mitigation is that it is used only by the provisioning
  path.

Getting realm-wide user management off the per-request path needs a different membership
mechanism (membership granted by the organization's IdP at broker login, or invitations), not
a permission. That is a design question for the B2B onboarding project and is not settled
here.

CI trial, on 26.7.4, before any design is fixed:

1. `admin_permissions_enabled=True` on the olapps realm in CI.
2. Create a test client with no roles and an Organizations `manage` permission on one
   organization. There is no Pulumi resource for this permission type (M8), so create it
   through the Admin API for the trial.
3. Add a Users `manage` permission, then call the Admin API operations mitxonline's
   membership path uses and record which succeed.
4. Confirm the existing role-based clients are unaffected by turning the realm switch on.

Declaring the Organizations permission in
Pulumi needs a provider resource that does not exist yet, so it is either an upstream request
or a scripted step until then.

`odl-video-app` holds `manage-users`, `view-users`, `query-users` and `query-groups` on the
same client users log in with. The comments say the roles are for the moira migration; the
migration code has shipped, but OVS reads groups and group members through the Admin API on
request and Celery paths, which needs `view-users` (M15). So (D14): drop `query-users` and
`query-groups` now, since every check they pass is also passed by `view-users`. Ask the OVS
owners whether `assign_group_users` is still used. If not, drop `manage-users`. If it is,
move it to a second service-account client that only the management command uses.  Direct access grants on
this client go in section 2.1, and the unread moira certificate in
`applications/odl_video_service/k8s_secrets.py:197-198` can be deleted with it.

## 4. 26.8 upgrade gates

Two gates. The first cleared on 2026-10-06, the second has not.

1. Cleared: pulumi-keycloak 6.14.0, released 2026-10-06, is built on
   terraform-provider-keycloak 5.10.0. Its organization-link layer
   (`keycloak/identity_provider_organization_compat.go`) only activates when the server
   reports 26.8.0 or later, so against 26.7.x it sends what 6.13.0 sends. On 26.8 it rejects
   `kc.org.*` keys passed through `extra_config`; we pass none, the IdPs use the typed
   `org_domain`, `organization_id` and `org_redirect_mode_email_matches` arguments
   (`olapps.py:923-925`, `org_sso_helpers.py:190-192,421-423`). `uv.lock` still pins 6.13.0.
   Bump it as its own PR now, expecting an empty `pulumi preview` on all three stacks, so the
   provider is not a variable during the server upgrade.
2. Open: a `kc-26.8` scim-for-keycloak jar in `s3://ol-eng-artifacts/keycloak/scim-client/`.
   The newest on 2026-10-06 is `kc-26.7-4.1.2`. The image pipeline selects the jar by
   major.minor and matches nothing otherwise.

Renovate has already opened the bump: ol-infrastructure PRs 6180 (`keycloak`) and 6181
(`keycloak-k8s-resources`), both green, neither set to automerge. They must not merge before
gate 2 clears. `renovate.json5` has no rule holding Keycloak; add a `packageRules` entry
limiting both packages to `26.7.x` in the same PR as the passkey fix, close those two PRs,
and remove the rule with the real bump.

Changes that ship with the bump:

- `--features-disabled=scim-api` in `ol-keycloak/Dockerfile.hosted` and
  `local-dev/keycloak/Dockerfile`. In 26.8 the built-in SCIM realm resource registers under
  the same provider id as the plugin and one silently replaces the other. At 26.7.4 `SCIM_API`
  is a preview feature and off, so the flag is not needed before the bump.
- The full-scope log category at ERROR (D13). Every client has full scope on, so the
  default would add a WARN per token issuance.
- `tk-check-dagster-keycloak-ingest-against-the-26-8-o-d0ee3f` resolved, since 26.8 drops
  columns Dagster reads.

Order within the upgrade: server stack to 26.8 in CI, then `pulumi preview` on
`substructure/keycloak` with the bumped provider, expecting no IdP replacement and no
organization-link diff. Then the behaviour checks in
`tk-check-the-26-8-behaviour-changes-that-touch-us-f-14e9a6`. Then QA, then a scheduled
Production window. The substructure pipeline must not run against a 26.8 server with the
6.13.0 provider at any point; pause it for the window in each environment.

## 5. What follows

These keep their existing tasks and are not redesigned here. The points below are constraints
the earlier sections put on them.

- Flows to built-ins (`332e4f`): applies to olapps and ol-mit. The staff realms are settled by
  D4 unless sign-off says otherwise. The olapps built-ins wrap the organization step in a
  condition where our copies do not, so compare behaviour for a user with no organization
  before switching.
- Client factory (`5ad1b8`): blocked on the client policy task. It should take PKCE, grant
  flags and `full_scope_allowed` as defaults so that section 2 becomes the default path.
- Authorization representation (`c37238`): read the live Vault admin role binding first. The
  role binds a `roles` claim that nothing in Pulumi emits.
- JWKS (`d80b95`): three consumers read `realm_public_key`. The other writes can be deleted
  with no consumer change.
- App-side tasks (`0a9b85`, `46d4fb`, `2dcd20`, `781fad`): independent of everything above
  except that `0a9b85` retires `mitlearn-admin-client` (D9).

## 6. Sequence

1. Passkey flow fix, CI to Production, with the Renovate hold in the same PR. No
   dependencies. It restores SSO in two realms and removes about 16.7k WARN lines a week
   before the upgrade adds its own.
2. pulumi-keycloak 6.14.0, alone, expecting no diff.
3. The clients that are ready (section 2.1): the two data-platform CLIs, Vault, Concourse,
   direct grants off on the three clients, the two redundant OVS roles. With them, the two
   marker policies.
4. `use_pkce` in the APISIX helper, then the gateway-fronted clients. Superset and
   OpenMetadata settings. These are independent of each other.
5. Changes in other repos, each its own task: witan CLI PKCE in agent-kit, a PKCE backend for
   ocw-studio and OVS.
6. FGAP trial in CI (section 3). Can run in parallel with 3 to 5.
7. mitxonline client split, after the trial and coordinated with the mitxonline change.
8. 26.8 upgrade, when the SCIM jar exists. Steps 3 to 7 do not block it; steps 1 and 2 should
   be done first.
9. Client factory and full scope, flows to built-ins for olapps and ol-mit, authorization
   representation, JWKS.
10. 26.8 feature trials, after the upgrade.

## 7. Still open

- Sign-off on D4, D10, D11, D12 and D14.
- Who created the organizations in the two staff realms and whether anything reads them.
- Why CI and QA run 26.7.5 while the pin and Production are 26.7.4.
- Why QA `ol-data-platform` is bound to `sh first broker login`.
- `secure-redirect-uris-enforcer` config values (section 2.2), from a CI trial.
- Whether client policies fire on Admin API updates of SAML clients. The source has no
  protocol check in the condition or the executors; it was not exercised.
- Grafana Cloud's SSO settings: `use_pkce`, and anything using the password grant.
- OpenMetadata's stored `authenticationConfiguration` row.
- Whether OVS still needs `assign_group_users`.
- Whether the vendor has published a kc-26.8 scim-for-keycloak build.
