# /// script
# requires-python = ">=3.12"
# dependencies = [
#   "asyncpg>=0.30",
#   "httpx>=0.28",
#   "pyjwt[crypto]>=2.10",
#   "pytest>=8",
#   "pytest-asyncio>=0.24",
# ]
# ///
"""Identity, sharing and deletion checks for the OL Hindsight extensions.

Runs against the harness in compose.yaml: a live Hindsight with the extensions
mounted, and a live Keycloak with the realm from keycloak.py. See README.md.
"""

import asyncio
import json
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

import asyncpg
import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

sys.path.insert(0, str(Path(__file__).parent))
import memory_client
from keycloak import (
    AUDIENCE,
    AUDIENCE_SCOPE,
    GENERAL_SCOPE,
    JWKS_URL,
    KEYCLOAK,
    REALM,
    create_learner,
    exchange_token,
    login_token,
    service_token,
)
from memory_client import ForgetClient, MemoryClient, bank_for

BASE = "http://127.0.0.1:8888"
APP_DSN = "postgresql://v-harness-app:harness-only-password@127.0.0.1:55432/hindsight"  # pragma: allowlist secret
SUPERUSER_DSN = "postgresql://postgres:harness-only-password@127.0.0.1:55432/hindsight"  # pragma: allowlist secret
GENERAL = "/v1/default/banks/g-courses"
NOT_FOUND = 404
FORBIDDEN = 403
UNAUTHORIZED = 401
CONFLICT = 409
UNPROCESSABLE = 422
ONE_PIXEL_PNG = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGMAAQAABQABDQottAAAAABJRU5ErkJggg=="
OK = 200
# Long enough for a worker task that slipped past the forget to finish writing.
# The notes in the race test are short: the race needs retain work in flight, and
# on a CPU embeddings server long notes turn it into a throughput test.
SETTLE_SECONDS = 10


@dataclass
class Learner:
    """A Keycloak user the tests act for."""

    id: str
    username: str

    def token(self, app: str, login_client: str | None = None) -> str:
        """Return what ``app`` holds after exchanging this learner's session token.

        :param app: The client performing the exchange.
        :param login_client: The client the learner logged in through, when it is
            not ``app``.
        """
        return exchange_token(app, login_token(login_client or app, self.username))

    @property
    def bank(self) -> str:
        """The learner's bank path."""
        return f"/v1/default/banks/{bank_for(self.id)}"


def new_learner() -> Learner:
    """Create a learner with a fresh, empty memory."""
    username = f"learner-{uuid.uuid4().hex[:12]}"
    return Learner(create_learner(username), username)


def http(token: str | None, **headers: str) -> httpx.AsyncClient:
    """Return a client for the API, authenticated with ``token`` if given."""
    if token:
        headers["authorization"] = f"Bearer {token}"
    return httpx.AsyncClient(base_url=BASE, headers=headers, timeout=60)


def admin_token() -> str:
    """Return the platform admin client's token."""
    return service_token("ol-memory-admin-client")


def general_token(client: str = "catalog-indexer") -> str:
    """Return a client-credentials token carrying the general-memory grant."""
    scope = None if client == "catalog-indexer" else f"{AUDIENCE_SCOPE} {GENERAL_SCOPE}"
    return service_token(client, scope=scope)


def foreign_signed_token() -> str:
    """Return a well-formed learner token signed by a key Keycloak never issued.

    It names Keycloak's own signing key id, so the check that fails is the
    signature, not the key lookup.
    """
    kid = httpx.get(JWKS_URL).json()["keys"][0]["kid"]
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = int(time.time())
    claims = {
        "iss": f"{KEYCLOAK}/realms/{REALM}",
        "aud": AUDIENCE,
        "azp": "learn-ai",
        "sub": str(uuid.uuid4()),
        "sid": str(uuid.uuid4()),
        "iat": now,
        "exp": now + 300,
    }
    return jwt.encode(claims, key, algorithm="RS256", headers={"kid": kid})


async def put_general(token: str, key: str, text: str, app: str) -> httpx.Response:
    """Write a note into the general bank."""
    async with http(token) as c:
        return await c.post(
            f"{GENERAL}/memories",
            json={
                "items": [
                    {
                        "content": text,
                        "document_id": f"{app}.{key}",
                        "tags": [f"app:{app}"],
                    }
                ]
            },
        )


@pytest.fixture
async def seeded():
    """Two learners; learn-ai and mit-learn each write a note for the first."""
    a, b = new_learner(), new_learner()
    lai, mle = MemoryClient(BASE, "learn-ai"), MemoryClient(BASE, "mit-learn")
    await lai.put(
        a.token("learn-ai"), "pace", "The learner has 30 minutes per weekday."
    )
    await mle.put(
        a.token("mit-learn"), "goal", "The learner wants a data science certificate."
    )
    await lai.put(b.token("learn-ai"), "pace", "Learner B studies on weekends.")
    yield a, b, lai, mle
    await lai.aclose()
    await mle.aclose()


@pytest.fixture
async def general_note():
    """Write a note into the general bank under the general grant."""
    key = f"course-{uuid.uuid4().hex[:8]}"
    r = await put_general(
        general_token(), key, f"Course {key} runs every spring term.", "catalog-indexer"
    )
    assert r.status_code == OK, r.text
    return key


# -- authentication -----------------------------------------------------------------


def _bad_tokens() -> dict[str, str | None]:
    a = new_learner()
    return {
        "no-token": None,
        "learner-login-token-not-exchanged": login_token("learn-ai", a.username),
        "app-service-account": service_token("learn-ai"),
        "app-service-account-with-audience": service_token(
            "learn-ai", scope=AUDIENCE_SCOPE
        ),
        "exchanged-by-unlisted-client": a.token("other-app"),
        "foreign-signer": foreign_signed_token(),
        "tampered-signature": a.token("learn-ai")[:-6] + "AAAAAA",
    }


BAD_TOKENS = _bad_tokens()


@pytest.mark.parametrize("name", list(BAD_TOKENS))
async def test_rejects_anything_but_an_exchanged_learner_token(name):
    """Every token but one exchanged for the service by a listed app is a 401."""
    async with http(BAD_TOKENS[name]) as c:
        r = await c.post(
            "/v1/default/banks/u-00000000-0000-0000-0000-000000000000/memories/recall",
            json={"query": "x"},
        )
    assert r.status_code == UNAUTHORIZED, (name, r.text)


async def test_learner_header_cannot_redirect_an_app_token(seeded):
    """An app cannot name a different learner with the admin path's header."""
    a, b, _, _ = seeded
    async with http(a.token("learn-ai"), **{"x-ol-learner": b.id}) as c:
        r = await c.get(f"{b.bank}/memories/list")
    assert r.status_code == NOT_FOUND, r.text


async def test_app_exchanges_a_token_from_the_shared_login_client():
    """An app exchanges, as itself, a token issued to the client it shares a login with."""
    # learn-ai's routes log learners in through mit-learn's client. With learn-ai
    # in that client's token audience, learn-ai exchanges as itself and its
    # writes carry its own name.
    a = new_learner()
    token = a.token("learn-ai", login_client="mit-learn")
    assert jwt.decode(token, options={"verify_signature": False})["azp"] == "learn-ai"
    lai = MemoryClient(BASE, "learn-ai")
    await lai.put(token, "pace", "Studies before work.")
    assert [(n.app, n.key) for n in await lai.get(token)] == [("learn-ai", "pace")]
    await lai.aclose()


async def test_sub_comes_from_the_memory_audience_scope():
    """App session tokens carry no sub; the exchanged memory token does."""
    # The olapps app clients lack Keycloak's basic scope, so the learner id
    # reaches the service only through the audience scope's own sub mapper.
    a = new_learner()
    login = jwt.decode(
        login_token("mit-learn", a.username), options={"verify_signature": False}
    )
    exchanged = jwt.decode(a.token("mit-learn"), options={"verify_signature": False})
    assert "sub" not in login, login
    assert exchanged["sub"] == a.id, exchanged


# -- per-learner isolation ----------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("post", "/memories/recall", {"query": "schedule"}),
        ("get", "/memories/list", None),
        ("get", "/documents", None),
        ("get", "/documents/learn-ai.pace", None),
        (
            "post",
            "/memories",
            {"items": [{"content": "injected", "tags": ["app:learn-ai"]}]},
        ),
        ("get", "/entities", None),
        ("get", "/tags", None),
        ("get", "/mental-models", None),
        ("delete", "/documents/learn-ai.pace", None),
    ],
)
async def test_cannot_touch_another_learners_bank(seeded, method, path, body):
    """Another learner's bank reads as missing on every endpoint."""
    a, b, _, _ = seeded
    async with http(a.token("learn-ai")) as c:
        kwargs = {"json": body} if body is not None else {}
        r = await getattr(c, method)(f"{b.bank}{path}", **kwargs)
    assert r.status_code == NOT_FOUND, (path, r.status_code, r.text)


async def test_bank_list_shows_only_own_bank(seeded):
    """The bank list shows only the token's own learner."""
    a, _, _, _ = seeded
    async with http(a.token("mit-learn")) as c:
        r = await c.get("/v1/default/banks")
    assert {bank["bank_id"] for bank in r.json()["banks"]} <= {bank_for(a.id)}, r.text


async def test_cannot_read_memory_or_chunk_of_other_learner_by_id(seeded):
    """Another learner's memory and chunk ids are unreadable."""
    a, b, _, _ = seeded
    async with http(b.token("learn-ai")) as c:
        item = (await c.get(f"{b.bank}/memories/list")).json()["items"][0]
    async with http(a.token("learn-ai")) as c:
        by_id = await c.get(f"{b.bank}/memories/{item['id']}")
        chunk = await c.get(f"/v1/default/chunks/{item['chunk_id']}")
    assert by_id.status_code == NOT_FOUND, by_id.text
    assert chunk.status_code in {FORBIDDEN, NOT_FOUND}, chunk.text


# -- one shared memory per learner, writes stamped with the writing app ---------------


async def test_every_app_reads_the_whole_learner_memory(seeded):
    """Every app reads and searches every app's notes for the learner."""
    a, _, lai, mle = seeded
    for client, app in ((lai, "learn-ai"), (mle, "mit-learn")):
        notes = await client.get(a.token(app))
        assert {(n.app, n.key) for n in notes} == {
            ("learn-ai", "pace"),
            ("mit-learn", "goal"),
        }, (app, notes)
    found = await mle.search(
        a.token("mit-learn"), "how much time does the learner have each weekday?"
    )
    assert any(n.app == "learn-ai" and "30 minutes" in n.text for n in found), found


@pytest.mark.parametrize(
    "tags", [[], ["app:mit-learn"], ["app:learn-ai", "app:mit-learn"]]
)
async def test_app_can_only_stamp_its_own_name(seeded, tags):
    """A write tagged with any app but the caller is refused."""
    a, _, _, _ = seeded
    async with http(a.token("learn-ai")) as c:
        r = await c.post(
            f"{a.bank}/memories",
            json={
                "items": [{"content": "x", "document_id": "learn-ai.y", "tags": tags}]
            },
        )
    assert r.status_code == FORBIDDEN, (tags, r.text)


async def test_app_cannot_overwrite_or_delete_another_apps_note(seeded):
    """Only the app that wrote a note can replace or delete it."""
    a, _, _, _ = seeded
    async with http(a.token("mit-learn")) as c:
        put = await c.post(
            f"{a.bank}/memories",
            json={
                "items": [
                    {
                        "content": "overwritten",
                        "document_id": "learn-ai.pace",
                        "tags": ["app:mit-learn"],
                    }
                ]
            },
        )
        delete = await c.delete(f"{a.bank}/documents/learn-ai.pace")
    async with http(a.token("learn-ai")) as c:
        still = (
            await c.get(
                f"{a.bank}/memories/list", params={"document_id": "learn-ai.pace"}
            )
        ).json()["items"]
    assert (put.status_code, delete.status_code) == (FORBIDDEN, FORBIDDEN)
    assert [i["text"] for i in still] == ["The learner has 30 minutes per weekday."]


@pytest.mark.parametrize(
    "item",
    [
        {"content": "x", "document_id": "mit-learn.y", "tags": ["app:learn-ai"]},
        {"content": "x", "document_id": "y", "tags": ["app:learn-ai"]},
        {"content": "x", "tags": ["app:learn-ai"]},
    ],
)
async def test_app_document_ids_carry_its_own_name(seeded, item):
    """An app cannot write under a key that names another app, or no app."""
    a, _, _, _ = seeded
    async with http(a.token("learn-ai")) as c:
        r = await c.post(f"{a.bank}/memories", json={"items": [item]})
    assert r.status_code == FORBIDDEN, (item, r.text)


@pytest.mark.parametrize("tags", [[], ["app:mit-learn"]])
async def test_app_cannot_retag_its_note_away_from_itself(seeded, tags):
    """A retag that would drop the app's own tag is refused."""
    a, _, lai, _ = seeded
    async with http(a.token("learn-ai")) as c:
        r = await c.patch(f"{a.bank}/documents/learn-ai.pace", json={"tags": tags})
    assert r.status_code == FORBIDDEN, (tags, r.text)
    notes = await lai.get(a.token("learn-ai"))
    assert ("learn-ai", "pace") in {(n.app, n.key) for n in notes}, notes


@pytest.mark.parametrize(
    "body",
    [
        {"text": "Email me at a.learner@example.org"},
        {"state": "invalidated", "reason": "call +1 617 555 0142"},
    ],
)
async def test_app_cannot_edit_a_memory_unit(seeded, body):
    """Unit edits bypass redaction, so apps replace a note by retaining it."""
    a, _, _, _ = seeded
    async with http(a.token("learn-ai")) as c:
        [unit] = (
            await c.get(
                f"{a.bank}/memories/list", params={"document_id": "learn-ai.pace"}
            )
        ).json()["items"]
        r = await c.patch(f"{a.bank}/memories/{unit['id']}", json=body)
        [after] = (
            await c.get(
                f"{a.bank}/memories/list", params={"document_id": "learn-ai.pace"}
            )
        ).json()["items"]
    assert r.status_code == FORBIDDEN, (body, r.text)
    assert after["text"] == "The learner has 30 minutes per weekday.", after


async def test_get_reads_past_one_page(seeded, monkeypatch):
    """get() returns every note, not only the first page."""
    a, _, lai, _ = seeded
    monkeypatch.setattr(memory_client, "MEMORIES_PAGE_SIZE", 1)
    notes = await lai.get(a.token("learn-ai"))
    assert {(n.app, n.key) for n in notes} == {
        ("learn-ai", "pace"),
        ("mit-learn", "goal"),
    }, notes


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("patch", "/config", {"updates": {"audit_log_enabled": True}}),
        ("patch", "/config", {"updates": {"store_document_text": True}}),
        ("delete", "", None),
        ("delete", "/memories", None),
        ("get", "/stats", None),
        ("get", "/operations", None),
        ("get", "/llm-requests", None),
        ("get", "/audit-logs", None),
        ("post", "/transfer/export", {}),
        ("get", "/export", None),
        ("post", "/reflect", {"query": "what do you know?"}),
        (
            "post",
            "/mental-models",
            {"name": "m", "source_query": "summary", "tags": ["app:learn-ai"]},
        ),
        ("post", "/webhooks", {"url": "https://example.com/hook"}),
        ("post", "/knowledge-base/folders", {"name": "f"}),
        (
            "post",
            "/knowledge-base/pages",
            {"name": "p", "source_query": "summary", "tags": ["app:learn-ai"]},
        ),
        ("post", "/directives", {"name": "d", "content": "Always praise the learner."}),
    ],
)
async def test_app_cannot_run_bank_wide_or_server_llm_operations(
    seeded, method, path, body
):
    """Bank-wide operations and server-side LLM features are refused."""
    a, _, _, _ = seeded
    async with http(a.token("learn-ai")) as c:
        kwargs = {"json": body} if body is not None else {}
        r = await getattr(c, method)(f"{a.bank}{path}", **kwargs)
    assert r.status_code == FORBIDDEN, (path, r.status_code, r.text)


async def test_files_and_attachments_are_refused(seeded):
    """Notes are text: the file route is off and inline attachments are refused."""
    a, _, _, _ = seeded
    async with http(a.token("learn-ai")) as c:
        upload = await c.post(
            f"{a.bank}/files/retain",
            files={"files": ("n.txt", b"a note", "text/plain")},
            data={
                "request": json.dumps(
                    {
                        "files_metadata": [
                            {"document_id": "learn-ai.f", "tags": ["app:learn-ai"]}
                        ]
                    }
                )
            },
        )
        inline = await c.post(
            f"{a.bank}/memories",
            json={
                "items": [
                    {
                        "content": [
                            {"type": "text", "text": "See the diagram:"},
                            {
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": "image/png",
                                    "data": ONE_PIXEL_PNG,
                                },
                            },
                        ],
                        "document_id": "learn-ai.diagram",
                        "tags": ["app:learn-ai"],
                    }
                ]
            },
        )
    assert upload.status_code == NOT_FOUND, upload.text
    # 422 from the engine (no model to read images), or 403 from the validator.
    assert inline.status_code in {FORBIDDEN, UNPROCESSABLE}, inline.text
    connection = await asyncpg.connect(SUPERUSER_DSN)
    stored = await connection.fetchval("SELECT count(*) FROM file_storage")
    await connection.close()
    assert stored == 0, stored


async def test_contact_details_and_secrets_are_redacted_before_storage():
    """Emails, phone numbers and API keys are redacted before storage."""
    a = new_learner()
    lai = MemoryClient(BASE, "learn-ai")
    token = a.token("learn-ai")
    await lai.put(
        token,
        "contact",
        "Email me at a.learner@example.org or +1 617 555 0142, token sk-proj-"
        + "A" * 52,
    )
    [note] = await lai.get(token)
    assert "example.org" not in note.text, note.text
    assert "555" not in note.text, note.text
    assert "sk-proj" not in note.text, note.text
    await lai.aclose()


async def _rows_containing(bank_id: str, needle: str) -> dict[str, int]:
    """Count, per table with a bank_id, the bank's rows whose text has ``needle``."""
    connection = await asyncpg.connect(SUPERUSER_DSN)
    tables = await connection.fetch(
        "SELECT c.table_name FROM information_schema.columns c"
        " JOIN information_schema.tables t USING (table_schema, table_name)"
        " WHERE c.table_schema = 'public' AND c.column_name = 'bank_id'"
        " AND t.table_type = 'BASE TABLE'"
    )
    found = {}
    for (table,) in tables:
        count = await connection.fetchval(
            f'SELECT count(*) FROM public."{table}" t'  # noqa: S608
            " WHERE t.bank_id = $1 AND t::text LIKE '%' || $2 || '%'",
            bank_id,
            needle,
        )
        if count:
            found[table] = count
    await connection.close()
    return found


@pytest.mark.parametrize("run_async", [False, True])
async def test_every_retain_field_is_redacted_before_storage(run_async):
    """Context, metadata and entities are redacted too, sync or async."""
    # Memory Defense screens only content, in the worker; an async retain's
    # items are stored in its operation row before the worker runs.
    a = new_learner()
    async with http(a.token("learn-ai")) as c:
        r = await c.post(
            f"{a.bank}/memories",
            json={
                "async": run_async,
                "items": [
                    {
                        "content": "Reach me at c.learner@example.org.",
                        "context": "From c.learner@example.org",
                        "metadata": {"contact": "c.learner@example.org"},
                        "entities": [{"text": "c.learner@example.org"}],
                        "document_id": "learn-ai.contact",
                        "tags": ["app:learn-ai"],
                    }
                ],
            },
        )
        assert r.status_code == OK, r.text
        if run_async:
            await asyncio.sleep(SETTLE_SECONDS)
    assert await _rows_containing(bank_for(a.id), "example.org") == {}


# -- general memory: a separate grant, readable by every app ---------------------------


async def test_general_grant_writes_and_every_app_reads_with_a_learner_token(
    general_note,
):
    """General memory is written under the grant and read by every app."""
    a = new_learner()
    for app in ("learn-ai", "mit-learn"):
        async with http(a.token(app)) as c:
            r = await c.get(f"{GENERAL}/memories/list", params={"limit": 100})
        assert r.status_code == OK, (app, r.text)
        assert any(general_note in item["text"] for item in r.json()["items"]), app


async def test_general_grant_can_be_held_as_an_optional_scope():
    """An app holding the grant as an optional scope writes general memory."""
    r = await put_general(
        general_token("learn-ai"),
        "faq",
        "Certificates are issued within a week.",
        "learn-ai",
    )
    assert r.status_code == OK, r.text


async def test_learner_token_cannot_write_general_memory_even_with_the_scope():
    """A learner token never writes general memory, even carrying the scope."""
    a = new_learner()
    exchanged = exchange_token(
        "learn-ai",
        login_token("learn-ai", a.username),
        scope=f"{AUDIENCE_SCOPE} {GENERAL_SCOPE}",
    )
    claims = jwt.decode(exchanged, options={"verify_signature": False})
    assert GENERAL_SCOPE in claims["scope"].split()
    r = await put_general(exchanged, "leak", "Something a learner said.", "learn-ai")
    assert r.status_code == FORBIDDEN, r.text


async def test_service_token_without_the_grant_is_refused():
    """A client-credentials token without the grant is a 401."""
    async with http(service_token("mit-learn", scope=AUDIENCE_SCOPE)) as c:
        r = await c.get(f"{GENERAL}/memories/list")
    assert r.status_code == UNAUTHORIZED, r.text


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("get", "/memories/list", None),
        ("post", "/memories/recall", {"query": "schedule"}),
        (
            "post",
            "/memories",
            {"items": [{"content": "injected", "tags": ["app:catalog-indexer"]}]},
        ),
        ("delete", "/documents/learn-ai.pace", None),
    ],
)
async def test_general_grant_cannot_reach_a_learner_bank(seeded, method, path, body):
    """The general grant never reaches a learner's bank."""
    a, _, _, _ = seeded
    for bank in (a.bank, "/v1/default/banks/u-general"):
        async with http(general_token()) as c:
            kwargs = {"json": body} if body is not None else {}
            r = await getattr(c, method)(f"{bank}{path}", **kwargs)
        assert r.status_code == NOT_FOUND, (bank, path, r.status_code, r.text)


async def test_general_writer_cannot_overwrite_another_apps_general_note(general_note):
    """General notes keep the same per-app ownership as learner notes."""
    r = await put_general(
        general_token("learn-ai"), general_note, "Overwritten.", "catalog-indexer"
    )
    assert r.status_code == FORBIDDEN, r.text
    async with http(general_token("learn-ai")) as c:
        r = await c.delete(f"{GENERAL}/documents/catalog-indexer.{general_note}")
    assert r.status_code == FORBIDDEN, r.text


@pytest.mark.parametrize("bank", ["g-Courses", "g-", "x-courses", "courses"])
async def test_only_well_formed_general_banks_exist(bank):
    """Bank ids outside the learner and general shapes are unreachable."""
    async with http(general_token()) as c:
        r = await c.post(
            f"/v1/default/banks/{bank}/memories",
            json={"items": [{"content": "x", "tags": ["app:catalog-indexer"]}]},
        )
    assert r.status_code == NOT_FOUND, (bank, r.text)


async def test_general_writer_lists_no_banks(general_note):  # noqa: ARG001
    """A general-grant caller lists no banks."""
    async with http(general_token()) as c:
        r = await c.get("/v1/default/banks")
    assert r.json()["banks"] == [], r.text


# -- the admin client: forget only -------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("post", "/memories/recall", {"query": "x"}),
        ("get", "/memories/list", None),
        ("post", "/memories", {"items": [{"content": "x", "document_id": "admin.x"}]}),
        ("get", "/documents", None),
        ("get", "/export", None),
    ],
)
async def test_admin_client_cannot_read_or_write_memory(seeded, method, path, body):
    """The admin client cannot read or write memory."""
    a, _, _, _ = seeded
    async with http(admin_token(), **{"x-ol-learner": a.id}) as c:
        kwargs = {"json": body} if body is not None else {}
        r = await getattr(c, method)(f"{a.bank}{path}", **kwargs)
    assert r.status_code == FORBIDDEN, (path, r.status_code, r.text)


async def test_admin_client_lists_no_banks(seeded):
    """The admin client lists no banks, not even the learner it names."""
    a, _, _, _ = seeded
    async with http(admin_token(), **{"x-ol-learner": a.id}) as c:
        r = await c.get("/v1/default/banks")
    assert r.status_code == OK, r.text
    assert r.json()["banks"] == [], r.text


async def test_learner_token_cannot_use_the_admin_path(seeded):
    """Only the admin client deletes a bank."""
    a, _, _, _ = seeded
    async with http(a.token("learn-ai"), **{"x-ol-learner": a.id}) as c:
        r = await c.delete(f"{a.bank}")
    assert r.status_code == FORBIDDEN, r.text


async def test_forget_removes_every_app_and_fences(seeded):
    """Forget removes every app's notes and refuses a later write."""
    a, b, lai, mle = seeded
    forget = ForgetClient(BASE, admin_token())
    assert await forget.forget_user(a.id) > 0
    assert await lai.get(a.token("learn-ai")) == []
    assert await mle.get(a.token("mit-learn")) == []
    with pytest.raises(httpx.HTTPStatusError) as err:
        await lai.put(a.token("learn-ai"), "pace", "late write after forget")
    assert err.value.response.status_code == CONFLICT
    assert len(await lai.get(b.token("learn-ai"))) == 1
    await forget.aclose()


async def test_store_refuses_a_write_to_a_forgotten_bank_from_any_writer(seeded):
    """A write that never goes through the API is still refused once forgotten."""
    # Hindsight can keep writing from a task that already failed, after the
    # validator's only check; the trigger is what stops that.
    a, _, _, _ = seeded
    forget = ForgetClient(BASE, admin_token())
    await forget.forget_user(a.id)
    await forget.aclose()
    connection = await asyncpg.connect(APP_DSN)
    with pytest.raises(asyncpg.CheckViolationError):
        await connection.execute(
            "INSERT INTO banks (bank_id) VALUES ($1)", bank_for(a.id)
        )
    await connection.close()


async def test_forget_prunes_expired_fences(seeded):
    """A forget clears fences that have expired, so no bank id outlives one."""
    a, _, _, _ = seeded
    stale = bank_for(str(uuid.uuid4()))
    connection = await asyncpg.connect(SUPERUSER_DSN)
    await connection.execute(
        "INSERT INTO ol_memory_bank_fence VALUES ($1, now() - interval '1 minute')",
        stale,
    )
    forget = ForgetClient(BASE, admin_token())
    await forget.forget_user(a.id)
    await forget.aclose()
    remaining = await connection.fetch(
        "SELECT bank_id FROM ol_memory_bank_fence WHERE bank_id = ANY($1::text[])",
        [stale, bank_for(a.id)],
    )
    await connection.close()
    assert [row["bank_id"] for row in remaining] == [bank_for(a.id)]


@pytest.mark.parametrize("delay", [0.0, 0.1, 0.3, 0.5, 0.8, 1.0, 1.5, 2.0, 3.0, 5.0])
async def test_forget_with_async_retain_in_flight_leaves_nothing(delay):
    """Forget racing an async retain leaves no rows behind."""
    a = new_learner()
    forget = ForgetClient(BASE, admin_token())
    async with http(a.token("learn-ai")) as c:
        items = [
            {
                "content": f"note {i} " + "word " * 60,
                "document_id": f"learn-ai.n{i}",
                "tags": ["app:learn-ai"],
            }
            for i in range(40)
        ]
        r = await c.post(f"{a.bank}/memories", json={"items": items, "async": True})
        assert r.status_code == OK, r.text
    await asyncio.sleep(delay)
    await forget.forget_user(a.id)
    await asyncio.sleep(SETTLE_SECONDS)
    connection = await asyncpg.connect(SUPERUSER_DSN)
    leftover = await connection.fetchval(
        """
        SELECT (SELECT count(*) FROM banks WHERE bank_id = $1)
             + (SELECT count(*) FROM memory_units WHERE bank_id = $1)
             + (SELECT count(*) FROM documents WHERE bank_id = $1)
             + (SELECT count(*) FROM entities WHERE bank_id = $1)
             + (SELECT count(*) FROM llm_requests WHERE bank_id = $1)
        """,
        bank_for(a.id),
    )
    await connection.close()
    assert leftover == 0, json.dumps({"bank": bank_for(a.id), "rows": leftover})
    await forget.aclose()


if __name__ == "__main__":
    sys.exit(
        pytest.main(
            [
                __file__,
                "-q",
                "-p",
                "no:cacheprovider",
                "-o",
                "asyncio_mode=auto",
                *sys.argv[1:],
            ]
        )
    )
