# `witan-desktop` — connecting GUI MCP clients to witan

Covers adding witan as a remote MCP connector from a desktop/web app that has
no CLI and no device-code support — Claude Desktop, the ChatGPT/Codex desktop
app. Terminal tools (`witan` itself, Codex CLI, Gemini CLI) already have a
working path through `witan-cli`'s device-code grant
(`docs/witan-service-account-runbook.md` covers the service-account side of
that realm; see agent-kit ADR-0005 for the CLI flow itself) — nothing here
duplicates that, and nothing here is needed to use witan from a terminal.

## Why this exists as a separate client

None of the apps below can do RFC 8628 device-code login, and this realm has
Dynamic Client Registration and CIMD (Client ID Metadata Document) both
unavailable to them — DCR is deliberately disabled realm-wide, and CIMD is
blocked upstream on `keycloak/keycloak#51413` (needs RFC 8707 resource
indicators to stamp `aud: witan` onto a token for a client Keycloak
provisions on the fly; tracked for milestone 26.8.0, not shipped as of
KEYCLOAK_VERSION 26.7.2). So `witan-desktop` is a static, Pulumi-managed
PUBLIC client (PKCE required, no secret) with one `valid_redirect_uris` entry
per app — the same shared-client pattern as `toolhive-swe-cli`.

Revisit once CIMD + resource indicators ship: at that point the fixed
redirect-URI list below could be replaced with each vendor's own hosted
metadata document instead.

## Connection details

- **MCP endpoint:** `https://witan.<env>.ol.mit.edu/mcp` (Production:
  `https://witan.ol.mit.edu/mcp`)
- **Issuer:** `https://sso-<env>.ol.mit.edu/realms/ol-platform-engineering`
  (Production: `https://sso.ol.mit.edu/realms/ol-platform-engineering`) — note
  the hyphen for non-production, not a dot; matches `KEYCLOAK_DOMAIN` in
  `src/ol_infrastructure/applications/witan/__main__.py:278-281`.
- **Client ID:** `witan-desktop`
- **Client secret:** none — PUBLIC client, PKCE (S256) only
- Access requires membership in the `ol-platform-engineering` realm; realm
  membership is the entire witan authorization boundary (see the `WITAN`
  block in `src/ol_infrastructure/substructure/keycloak/ol_platform_engineering.py`).

## Claude Desktop

1. Settings → Connectors → Add custom connector.
2. Server URL: the MCP endpoint above.
3. Advanced settings → OAuth Client ID: `witan-desktop`. Leave OAuth Client
   Secret blank.
4. Complete the browser login when prompted; Claude's own callback
   (`https://claude.ai/api/mcp/auth_callback`) is already registered on the
   `witan-desktop` client.

## ChatGPT / Codex desktop app

Connector setup happens in the ChatGPT web app first — the desktop app reuses
whatever connection is configured there, it doesn't have its own separate
connector UI.

1. In the ChatGPT web app: Settings → Connectors → Advanced → Developer mode
   → Add custom connector.
2. Server URL: the MCP endpoint above.
3. Provide client ID `witan-desktop`, no secret.
4. Complete the browser login; the redirect targets already registered on
   `witan-desktop` (`chatgpt.com/oauth/callback`,
   `chatgpt.com/connector_platform_oauth_redirect`,
   `chat.openai.com/oauth/callback`) are candidates sourced from web search,
   not verified against a published OpenAI spec or an end-to-end login — if
   login fails with an invalid-redirect error, capture the exact
   `redirect_uri` ChatGPT sent and add it to `valid_redirect_uris` on the
   `witan-desktop` client.
5. Reopen the Codex surface in the desktop app; it should pick up the
   connection made in step 1–4.

## Gemini

Not wired up. Google's remote-MCP OAuth model for the Gemini app
(Gemini Enterprise) is admin-console-driven per tenant, not a fixed public
callback the way Claude's and ChatGPT's are — there's no single
`valid_redirect_uris` value to register ahead of time. Revisit if/when a
concrete redirect URI is confirmed.

## Adding another app

1. Find the app's OAuth redirect URI for remote MCP connectors — prefer the
   vendor's own docs; if none exist, capture the value the app actually sends
   during a failed login attempt (Keycloak's server log records the rejected
   `redirect_uri` at WARN).
2. Add it to `valid_redirect_uris` on
   `ol-platform-engineering-witan-desktop-client` in `ol_platform_engineering.py`.
3. No mapper changes needed — `witan-desktop`'s `AudienceProtocolMapper`
   already stamps `aud: witan` on every token this client mints, regardless
   of which redirect URI was used.

## Related

- `docs/adr/0009-deploy-witan-as-shared-multi-tenant-mcp-service.md` — the
  auth-model ADR this client fits into (JWT validated directly by witan, no
  broker in front of it).
- agent-kit `mcp/servers/witan/docs/adr/0005-secure-cli-path-into-deployed-witan.md`
  — the CLI-side counterpart (`witan-cli`, device-code grant) this doc is
  explicitly not duplicating.
