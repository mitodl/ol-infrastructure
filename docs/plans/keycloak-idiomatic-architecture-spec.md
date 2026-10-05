# Keycloak audit fixes and 26.8 upgrade: sequencing and design spec

Status: spec, ready for review
Date: 2026-10-05
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
| D1 | Order: passkey flow fix, then per-client hygiene, then the client policy, then FGAP scoping in CI, then the 26.8 upgrade when both blockers clear, then the realm restructure. Section 6 has the dependency reasons. |
| D2 | Fix the passkey browser flow in place: make the passkey subflow ALTERNATIVE so all top-level siblings are the same kind. Do not wait for the return to built-in flows. |
| D3 | Drop the organization subflow from the two staff-realm browser flows. It has never executed, neither realm declares an organization, and turning it on would add an identity-first username form the realms have never shown. |
| D4 | Keep the staff realms on a custom flow with no password step. Do not switch them to the built-in browser flow. **Needs sign-off**, this amends `tk-return-the-hand-copied-browser-and-first-broker--332e4f`. |
| D5 | One helper builds the passkey browser flow for both staff realms, keeping today's Pulumi resource names and aliases. |
| D6 | Client policies are added after the clients conform, never before, and every executor runs with `auto-configure` off. |
| D7 | PKCE is enforced by policy on public clients first (`client-access-type` condition), once `witan-cli` is known to send PKCE on its device request. Confidential clients get `pkce_code_challenge_method="S256"` one at a time as each consumer is checked, and the condition widens to `any-client` when the last one is done. |
| D8 | Direct access grants and implicit flow are turned off per client first, then locked by `reject-ropc-grant` and `reject-implicit-grant`. |
| D9 | `mitlearn-admin-client` is not scoped with FGAP. It loses its roles when mit-learn stops using the Admin API (`tk-mit-learn-get-is-sso-user-from-a-token-claim-and-0a9b85`). |
| D10 | `mitxonline-b2b-client` is split in two: a runtime membership client with an Organizations permission and Users `manage`, and a provisioning client that keeps realm-wide `manage-identity-providers`. The split removes `manage-realm` and the IdP roles from the per-request path. It does not remove realm-wide user management, which FGAP v2 cannot scope for this use. The trial runs on 26.7.4 in CI. **Needs sign-off**, the scoping task's done-when cannot be met this way. |
| D11 | The FGAP work is done under this project. `wp-enable-fine-grained-admin-permissions-fgap-on-sh-73d6d3` (discovery, no tasks) should be closed in favour of it. **Needs sign-off.** |
| D12 | The DCC task leaves this project's sequence. Its two mitxonline fixes (refuse download of a revoked credential, https signer URL) do not depend on anything here and should go first. **Needs an owner.** |
| D13 | At the 26.8 upgrade, set the log category `org.keycloak.protocol.oidc.endpoints.TokenEndpoint.full-scope-allowed` to ERROR. Turning full scope off per client is restructure work and does not gate the upgrade. |

## Measured facts this rests on

Read from source or from the repo at `origin/main` 3abd43920 on 2026-10-05. Keycloak source was
read at the 26.7.4 tag, the version pinned at `src/bridge/lib/versions.py:12`.

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
the authenticator renders its own username form (`initialChallenge`). Whether either staff
realm has an organization created by hand was not read.

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
their live value is whatever the server holds. The built-in clients in each realm
(`admin-cli`, `security-admin-console`, `account-console` and the rest) are not in Pulumi and
are matched by `client-access-type` and `any-client` conditions like any other.

**M11. Adding an organization member needs two permissions.**
`OrganizationMemberResource.addMember` calls `auth.orgs().requireManage(organization)` and
`auth.users().requireManage(user)` (lines 106 and 110 at 26.7.4). The user is not yet a member
when the second check runs, so that permission cannot be limited to the organization's members.
26.7.4 also has `manage-organizations`, `view-organizations` and `query-organizations`
realm-management roles.

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
`ol-data-platform` the Touchstone button, and whether any staff user holds a password
credential is live state that was not read. Moving to
the built-in would make a password a valid way in for anyone who has one. The realm password
policy and `ol-grafana-client`'s direct access grants suggest some do. If sign-off prefers the
built-in, the precondition is an Admin API count of users with a `password` credential in each
realm, and a decision on deleting them.

Before applying in each environment, check the realm for hand-created organizations (M4). If
one exists, D3 still holds; it is only worth knowing why it is there.

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

The policy is the guard against regression. It cannot be the first step, because once a policy
matches a client the next Pulumi update of that client is validated (M6, M7) and fails the
deploy if the client does not conform. `auto-configure` would avoid the failure by rewriting
the client server-side during the update, and the rewritten field would then disagree with
Pulumi's inputs on the next refresh. That second half is an inference from M7, not something
observed; the rule (D6) costs nothing either way.

### 2.1 Per-client fixes first

| Client | Change | Check before applying |
|---|---|---|
| `ol-superset-cli`, `ol-starrocks-cli` | `pkce_code_challenge_method="S256"`; fixed loopback port in place of `http://localhost:*/callback`; explicit web origins | The CLI sends `code_challenge`. Comments at `applications/starrocks/__main__.py:845` and `substructure/starrocks/__main__.py:756` call `starrocks-auth` a PKCE script; the Superset CLI was not read. The port must match what each CLI binds. |
| `witan-cli` | `pkce_code_challenge_method="S256"` | The witan CLI sends `code_challenge` on its device authorization request and `code_verifier` on the token request. With the attribute set, Keycloak requires both (M7), so setting it against a CLI that does not would break every `witan-cli` login. The CLI is in the witan repo and was not read. The attribute is needed because `pkce-enforcer` validates it on every matching client. |
| `ol-open_metadata-client` | `implicit_flow_enabled=False` | OpenMetadata's configured OIDC response type. |
| `ol-superset-client`, `ol-grafana-client`, `odl-video-app` | `direct_access_grants_enabled=False` | Who uses the password grant. Loki cannot answer this (successful token events do not reach it with a grant type). Read each consumer's config and scripts, turn it off in CI and QA for a week, then Production. |
| `ol-mitlearn-client` | explicit web origins in place of `+` | The origins the Learn frontend calls from. |

### 2.2 Policy and profiles

Per realm, in a helper called once from each realm function:

- Profile `ol-pkce`: executor `pkce-enforcer`, `auto-configure: false`.
- Profile `ol-grants`: executors `reject-implicit-grant` and `reject-ropc-grant`,
  `auto-configure: false`.
- Profile `ol-redirects`: executor `secure-redirect-uris-enforcer`.
- Policy `ol-public-clients`: condition `client-access-type` = `public`; profiles `ol-pkce`,
  `ol-grants`, `ol-redirects`.
- Policy `ol-all-clients`: condition `any-client`; profiles `ol-grants`, then `ol-redirects`,
  then `ol-pkce` as each becomes true of every client.

The `secure-redirect-uris-enforcer` config is not settled here. Most redirect URIs end in `/*`
and the CLIs use `http` on loopback, so the starting point is `allow-wildcard-context-path`,
`allow-ipv4-loopback-address` and `allow-http-scheme` on, and `oauth-2-1-compliant` off
(it rejects `localhost` by name). A client with the standard flow on and no redirect URIs
fails validation outright. The executor does not catch `http://localhost:*/callback`; the
fixed port in section 2.1 is what removes that. Settle the values by applying the profile to
the CI realms and reading which client updates fail, then tighten wildcards as a later pass.

Two things to do before `ol-grants` reaches a realm:

- Read the live `directAccessGrantsEnabled` and `implicitFlowEnabled` of every client in the
  realm, not the Pulumi inputs (M10), and set the flags explicitly on the clients that omit
  them.
- Confirm nothing uses the password grant through `admin-cli` in the four realms. `admin-cli`
  is public with direct access grants on, so both policies match it and password grants
  through it will be rejected. The Pulumi provider is not affected; it authenticates to
  `master` with a client secret.

Confidential-client PKCE, consumer by consumer. None of these was verified, and which
integration each client uses is from the audit, not re-read:

| Consumer | Clients | What to confirm |
|---|---|---|
| APISIX `openid-connect` | Learn, MITx Online, learn-ai, analytics-api, and the gateway-fronted tools | `use_pkce` is set nowhere in `src/` today. Turning it on is a gateway config change per route. |
| python-social-auth | OCW Studio, OVS, open-discussions | Whether the Keycloak backend sends PKCE at the pinned version. |
| Tool-native OIDC | Vault, Concourse, Superset, OpenMetadata, Airbyte, Dagster, Leek, JupyterHub, Opik, Marimo, Gwarek | Each tool's own setting. |

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

`odl-video-app` keeps `manage-users`, `view-users`, `query-users` and `query-groups` for a
moira-to-Keycloak migration, on the same client users log in with. Confirm with the OVS owners
that the migration is finished, then remove the roles. This is the same change as turning off
its direct access grants in section 2.1.

## 4. 26.8 upgrade gates

Nothing here can start until both of these exist. Neither did on 2026-10-05.

1. A pulumi-keycloak release built on terraform-provider-keycloak 5.10.0 or later (latest is
   6.13.0). Without it the substructure stack sends `organization_id` and the removed
   `kc.org.*` config keys for every B2B organization IdP.
2. A `kc-26.8` scim-for-keycloak jar in `s3://ol-eng-artifacts/keycloak/scim-client/`. The
   image pipeline selects the jar by major.minor and matches nothing otherwise.

`KEYCLOAK_VERSION` carries a Renovate annotation, so a 26.8 bump PR will be proposed
regardless (the custom manager at `renovate.json5:78-87`). It must not merge before the gates
clear. `renovate.json5` has no rule holding Keycloak; add a `packageRules` entry limiting
`keycloak` and `keycloak-k8s-resources` to `26.7.x` in the same PR as the passkey fix, and
remove it with the bump.

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

1. Passkey flow fix, CI to Production. No dependencies. It restores SSO in two realms and
   removes about 16.7k WARN lines a week before the upgrade adds its own.
2. Per-client fixes in section 2.1. The two CLI clients and `witan-cli` first, since they are
   the precondition for the public-client policy.
3. `ol-public-clients` policy, then `ol-all-clients` with `ol-grants`. Every declared public
   client is in the two staff realms; in olapps and ol-mit the public-client policy matches
   only built-ins.
4. FGAP trial in CI (section 3). Can run in parallel with 2 and 3.
5. mitxonline client split, after the trial and coordinated with the mitxonline change.
6. 26.8 upgrade, whenever the two gates clear. Steps 1 to 3 do not block it; step 1 should be
   done first for the log baseline.
7. Client factory and full scope, flows to built-ins for olapps and ol-mit, authorization
   representation, JWKS.
8. 26.8 feature trials, after the upgrade.

## 7. Still open

- Sign-off on D4, D10, D11 and D12.
- Whether any staff-realm user has a password credential, and whether either staff realm has
  a hand-created organization. Both need Admin API reads against the live realms.
- `secure-redirect-uris-enforcer` config values (section 2.2).
- PKCE support for every confidential-client consumer (section 2.2).
- Who uses the password grant on the three clients that allow it.
- Whether the moira migration behind `odl-video-app`'s roles is finished.
- Whether the witan CLI sends PKCE on its device authorization request.
- Whether anything uses the password grant through `admin-cli` in the four realms.
- Live grant flags on the clients that do not declare them.
- Whether the vendor has published a kc-26.8 scim-for-keycloak build.
