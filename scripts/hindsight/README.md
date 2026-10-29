# Hindsight memory service harness

A local stack for testing the OL extensions to Hindsight
(`src/ol_infrastructure/applications/hindsight/ol_memory_ext`) before they
ship. It runs the deployed image with the deployed settings, a Postgres whose
login has only the privileges the Vault `app` role grants, a local embeddings
server in place of Bedrock, and a real Keycloak, then checks who can read, write and delete what.

What `test_isolation.py` covers:

- only a token exchanged for the service by a listed app is accepted, and the
  learner always comes from the token's `sub`;
- an app reaches the token's learner's bank alone, reads every app's notes
  there, and can change only its own;
- an app can exchange a token issued to the client it shares a login with,
  which is how learn-ai's routes work (they log in through mit-learn's client);
- general memory is written only with a client-credentials token carrying the
  `ol-memory-general` scope, is readable by every app, and the grant never
  reaches a learner's bank;
- server-side LLM features and bank-wide operations are refused;
- the admin client can only run the forget sequence, and a forget racing an
  async retain leaves no rows behind.

## Running it

From the repository root:

```shell
export HINDSIGHT_VERSION=$(< src/bridge/lib/version_pins/HINDSIGHT_VERSION)
export KEYCLOAK_VERSION=$(< src/bridge/lib/version_pins/KEYCLOAK_VERSION)
docker compose -f scripts/hindsight/compose.yaml up -d db keycloak embeddings
# Wait for Keycloak on 127.0.0.1:8098, then build the realm.
uv run --with httpx scripts/hindsight/keycloak.py
docker compose -f scripts/hindsight/compose.yaml up -d hindsight
# Wait for http://127.0.0.1:8888/health, then:
uv run --script scripts/hindsight/test_isolation.py
```

The forget-race cases take about two minutes; add `-k 'not in_flight'` to skip
them. `hindsight.env` mirrors `hindsight_settings` in the stack's
`__main__.py` except for the embeddings provider, and `init.sql` mirrors the Vault role statements there, so a
change to either belongs in both places.

Tear down with `docker compose -f scripts/hindsight/compose.yaml down -v`.
