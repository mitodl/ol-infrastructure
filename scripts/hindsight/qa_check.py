# /// script
# requires-python = ">=3.12"
# dependencies = [
#   "cyclopts>=3",
#   "httpx>=0.28",
#   "pyjwt[crypto]>=2.10",
# ]
# ///
"""End-to-end check of QA Hindsight with tokens exchanged from a real learner login.

Logs a learner in through ol-mitlearn-client (the client learn-ai's routes log
learners in through), exchanges that session token as learn-ai and as mit-learn
for the memory audience, and checks retain, recall, cross-app read, cross-app
overwrite and delete refusal, and, with --forget, the forget fence.

The client secrets come from QA Vault (secret-operations mount, kv-v1):
    MITLEARN_CLIENT_SECRET          sso/mitlearn          client_secret
    LEARN_AI_CLIENT_SECRET          sso/learn-ai          client_secret
    MITLEARN_MEMORY_CLIENT_SECRET   sso/mitlearn-memory   client_secret
    MEMORY_ADMIN_CLIENT_SECRET      sso/ol-memory-admin   client_secret (--forget only)
"""

import asyncio
import base64
import hashlib
import os
import secrets
import sys
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import httpx
import jwt
from cyclopts import App

sys.path.insert(0, str(Path(__file__).parent))
from keycloak import ACCESS_TOKEN_TYPE, AUDIENCE, AUDIENCE_SCOPE, EXCHANGE_GRANT
from memory_client import ForgetClient, MemoryClient, bank_for

KEYCLOAK_REALM_URL = "https://sso-qa.ol.mit.edu/realms/olapps"
TOKEN_URL = f"{KEYCLOAK_REALM_URL}/protocol/openid-connect/token"
AUTH_URL = f"{KEYCLOAK_REALM_URL}/protocol/openid-connect/auth"
# Any path under a registered wildcard; APISIX only redeems codes on its own
# callback path, so this code reaches the script unspent.
REDIRECT_URI = "https://rc.learn.mit.edu/__memory-qa-check"
LOGIN_CLIENT = "ol-mitlearn-client"
APPS = {"learn-ai": "ol-learn-ai-client", "mit-learn": "ol-mitlearn-memory-client"}
FORBIDDEN = 403
UNAUTHORIZED = 401
NOT_FOUND = 404
CONFLICT = 409

app = App(help=__doc__)
results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    """Record and print one check."""
    results.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}{f'  ({detail})' if detail else ''}")


def claims(token: str) -> dict[str, Any]:
    """Decode without verifying; the server is what verifies."""
    return jwt.decode(token, options={"verify_signature": False})


def audiences(token_claims: dict[str, Any]) -> list[str]:
    """Return ``aud`` as a list."""
    aud = token_claims.get("aud", [])
    return [aud] if isinstance(aud, str) else aud


def token_request(data: dict[str, str]) -> str:
    """POST to the token endpoint and return the access token."""
    response = httpx.post(TOKEN_URL, data=data, timeout=30)
    if response.is_error:
        msg = f"token request ({data['grant_type']}) failed: {response.text}"
        raise SystemExit(msg)
    return response.json()["access_token"]


def login(mitlearn_secret: str) -> str:
    """Run the authorization-code flow by hand and return the learner token."""
    verifier = secrets.token_urlsafe(64)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .rstrip(b"=")
        .decode()
    )
    state = secrets.token_urlsafe(16)
    url = (
        AUTH_URL
        + "?"
        + urlencode(
            {
                "client_id": LOGIN_CLIENT,
                "response_type": "code",
                "redirect_uri": REDIRECT_URI,
                "scope": "openid",
                "state": state,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            }
        )
    )
    print(
        "Open this URL and log in as a QA learner. The browser ends on mit-learn's\n"
        "404 page; paste the URL from its address bar (it starts with\n"
        "https://rc.learn.mit.edu/__memory-qa-check?) within five minutes, the\n"
        "realm's login code lifespan.\n"
    )
    print(url, "\n")
    pasted = input("Redirected URL or code: ").strip()
    query = parse_qs(urlparse(pasted).query) if pasted.startswith("http") else {}
    if query and query.get("state", [None])[0] != state:
        msg = "state does not match this login"
        raise SystemExit(msg)
    if query and "code" not in query:
        msg = f"the redirect carries no code: {query.get('error', query)}"
        raise SystemExit(msg)
    code = query["code"][0] if query else pasted
    return token_request(
        {
            "grant_type": "authorization_code",
            "client_id": LOGIN_CLIENT,
            "client_secret": mitlearn_secret,
            "code": code,
            "redirect_uri": REDIRECT_URI,
            "code_verifier": verifier,
        }
    )


def exchange(client: str, secret: str, subject_token: str) -> str:
    """Exchange the learner's session token for a memory-audience token."""
    return token_request(
        {
            "grant_type": EXCHANGE_GRANT,
            "client_id": client,
            "client_secret": secret,
            "subject_token": subject_token,
            "subject_token_type": ACCESS_TOKEN_TYPE,
            "requested_token_type": ACCESS_TOKEN_TYPE,
            "audience": AUDIENCE,
            "scope": AUDIENCE_SCOPE,
        }
    )


async def run(base_url: str, *, forget: bool) -> None:
    """Run every check against ``base_url``."""
    session = login(os.environ["MITLEARN_CLIENT_SECRET"])
    session_claims = claims(session)
    sid = session_claims.get("sid")
    aud = audiences(session_claims)
    check(
        "session token names both exchanging clients as audiences",
        set(APPS.values()) <= set(aud),
        f"aud={aud}",
    )
    # ol-mitlearn-client has no basic scope; the memory audience scope's own
    # sub mapper is what gives the exchanged token a sub.
    check(
        "session token carries no sub, and has a session id",
        "sub" not in session_claims and bool(sid),
        f"sub={session_claims.get('sub')} sid={sid}",
    )

    tokens = {
        "learn-ai": exchange(
            APPS["learn-ai"], os.environ["LEARN_AI_CLIENT_SECRET"], session
        ),
        "mit-learn": exchange(
            APPS["mit-learn"], os.environ["MITLEARN_MEMORY_CLIENT_SECRET"], session
        ),
    }
    exchanged = {name: claims(token) for name, token in tokens.items()}
    for name, c in exchanged.items():
        check(
            f"{name} exchange: sub set, the login's sid, azp {APPS[name]}, "
            f"aud {AUDIENCE}",
            bool(c.get("sub"))
            and c.get("sid") == sid
            and c["azp"] == APPS[name]
            and AUDIENCE in audiences(c),
            f"sub={c.get('sub')} sid={c.get('sid')} azp={c['azp']} aud={audiences(c)}",
        )
    sub = exchanged["learn-ai"].get("sub")
    if not sub:
        msg = "the learn-ai exchanged token has no sub"
        raise SystemExit(msg)
    check(
        "both apps' exchanged tokens name the same learner",
        exchanged["mit-learn"].get("sub") == sub,
    )
    print(f"\nlearner sub {sub}, bank {bank_for(sub)}\n")

    bank = f"/v1/default/banks/{bank_for(sub)}"
    async with httpx.AsyncClient(base_url=base_url, timeout=60) as raw:
        r = await raw.get(
            f"{bank}/memories/list", headers={"authorization": f"Bearer {session}"}
        )
        check(
            "un-exchanged session token is refused",
            r.status_code == UNAUTHORIZED,
            str(r.status_code),
        )

    lai = MemoryClient(base_url, "learn-ai")
    mle = MemoryClient(base_url, "mit-learn")
    key = f"qa-check-{uuid.uuid4().hex[:8]}"
    text = f"QA end-to-end check {key}: the learner prefers worked examples."
    try:
        await lai.put(tokens["learn-ai"], key, text)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == CONFLICT:
            msg = (
                "the bank is fenced by a forget in the last hour; "
                "use another learner or wait"
            )
            raise SystemExit(msg) from exc
        raise

    learn_ai_headers = {"authorization": f"Bearer {tokens['learn-ai']}"}
    async with httpx.AsyncClient(
        base_url=base_url, timeout=60, headers=learn_ai_headers
    ) as c:
        listed = await c.get(
            f"{bank}/memories/list", params={"document_id": f"learn-ai.{key}"}
        )
    items = listed.json()["items"] if listed.status_code == httpx.codes.OK else []
    check(
        "learn-ai retains a note under document id learn-ai.<key> and reads it back",
        len(items) == 1 and items[0]["text"] == text,
        f"{listed.status_code}, {len(items)} items",
    )
    if items:
        check(
            "the note's only app tag is app:learn-ai",
            [t for t in items[0]["tags"] if t.startswith("app:")] == ["app:learn-ai"],
            f"tags={items[0]['tags']}",
        )

    # Retain embeds asynchronously in some modes; give recall a few tries.
    found = []
    for _ in range(10):
        found = [
            n
            for n in await lai.search(tokens["learn-ai"], "worked examples")
            if n.key == key
        ]
        if found:
            break
        await asyncio.sleep(3)
    check("recall finds the note", bool(found))

    other = await mle.get(tokens["mit-learn"])
    check(
        "mit-learn reads learn-ai's note",
        any(n.key == key and n.app == "learn-ai" for n in other),
    )

    async with httpx.AsyncClient(
        base_url=base_url,
        timeout=60,
        headers={"authorization": f"Bearer {tokens['mit-learn']}"},
    ) as c:
        put = await c.post(
            f"{bank}/memories",
            json={
                "items": [
                    {
                        "content": "overwritten by mit-learn",
                        "document_id": f"learn-ai.{key}",
                        "tags": ["app:mit-learn"],
                    }
                ]
            },
        )
        delete = await c.delete(f"{bank}/documents/learn-ai.{key}")
    check(
        "mit-learn cannot overwrite or delete learn-ai's note",
        (put.status_code, delete.status_code) == (FORBIDDEN, FORBIDDEN),
        f"put={put.status_code} delete={delete.status_code}",
    )
    still = [n for n in await lai.get(tokens["learn-ai"]) if n.key == key]
    check("learn-ai's note is unchanged", bool(still) and still[0].text == text)

    if forget:
        admin = token_request(
            {
                "grant_type": "client_credentials",
                "client_id": "ol-memory-admin-client",
                "client_secret": os.environ["MEMORY_ADMIN_CLIENT_SECRET"],
            }
        )
        forgetter = ForgetClient(base_url, admin)
        removed = await forgetter.forget_user(sub)
        await forgetter.aclose()
        print(f"Forget completed; final attempt removed {removed} rows")
        async with httpx.AsyncClient(
            base_url=base_url, timeout=60, headers=learn_ai_headers
        ) as c:
            gone = await c.get(f"{bank}/memories/list")
        check(
            "forget deletes the bank (learn-ai's list gets 404)",
            gone.status_code == NOT_FOUND,
            str(gone.status_code),
        )
        try:
            await lai.put(tokens["learn-ai"], key, "late write after forget")
            check("a write after forget is refused (fence)", False, "accepted")
        except httpx.HTTPStatusError as exc:
            check(
                "a write after forget is refused (fence)",
                exc.response.status_code == CONFLICT,
                str(exc.response.status_code),
            )
    else:
        await lai.delete(tokens["learn-ai"], key)
        print("\n(forget skipped; the test note was deleted by learn-ai)")

    await lai.aclose()
    await mle.aclose()


@app.default
def main(base_url: str = "http://127.0.0.1:18888", *, forget: bool = False) -> None:
    """Run the checks.

    :param base_url: Hindsight API, e.g. through
        ``kubectl -n hindsight port-forward svc/hindsight 18888:8888``.
    :param forget: Also run the forget sequence and check the fence. This erases
        the learner's whole QA memory bank, every app's notes, and fences the
        bank for an hour.
    """
    asyncio.run(run(base_url, forget=forget))
    failed = [name for name, ok, _ in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    app()
