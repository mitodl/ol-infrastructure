"""The learner-scoped get/put/search/delete interface over the Hindsight API.

The shape ol-llm exposes (shared-llm-library-spec.md section 5.3), kept here
as the tests' client. A learner has one bank, shared by every app; each note
is stamped with the app that wrote it, and its key is its document id, so
``put`` with an existing key replaces the note.
"""

import asyncio
import re
import time
from dataclasses import dataclass

import httpx
import jwt

BANK_PREFIX = "u-"
DRAIN_POLL_SECONDS = 1.0
DRAIN_TIMEOUT_SECONDS = 600.0
FORGET_ATTEMPTS = 5
FORGET_BACKOFF_SECONDS = 2.0
HTTP_TIMEOUT_SECONDS = 30
OPERATIONS_PAGE_SIZE = 100
_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")


@dataclass(frozen=True)
class Note:
    """One stored note."""

    key: str
    text: str
    app: str
    score: float | None = None


def bank_for(learner_id: str) -> str:
    """Return the bank id of a learner.

    :param learner_id: The learner's Keycloak user id.
    :returns: The bank id.
    :rtype: str
    """
    return f"{BANK_PREFIX}{learner_id}"


def _note(item: dict[str, object], score: float | None = None) -> Note:
    app, _, key = str(item["document_id"]).partition(".")
    return Note(key=key, text=str(item["text"]), app=app, score=score)


class MemoryClient:
    """An app's view of learner memory.

    :param base_url: Hindsight API URL.
    :param app: The calling app's name, matching the tag the server stamps on its
        writes.
    """

    def __init__(self, base_url: str, app: str) -> None:
        """Create the client."""
        self.app = app
        self._http = httpx.AsyncClient(base_url=base_url, timeout=HTTP_TIMEOUT_SECONDS)

    async def aclose(self) -> None:
        """Close the underlying HTTP client."""
        await self._http.aclose()

    @staticmethod
    def _route(learner_token: str) -> tuple[str, dict[str, str]]:
        # An unverified decode, only to build the URL. The server verifies the
        # token and refuses any bank that is not its subject's.
        sub = jwt.decode(learner_token, options={"verify_signature": False})["sub"]
        return f"/v1/default/banks/{bank_for(sub)}", {
            "authorization": f"Bearer {learner_token}"
        }

    async def put(self, learner_token: str, key: str, text: str) -> None:
        """Store or replace the app's note ``key`` for the token's learner."""
        if not _KEY_RE.match(key):
            msg = f"invalid note key {key!r}"
            raise ValueError(msg)
        base, headers = self._route(learner_token)
        response = await self._http.post(
            f"{base}/memories",
            headers=headers,
            json={
                "items": [
                    {
                        "content": text,
                        "document_id": f"{self.app}.{key}",
                        "tags": [f"app:{self.app}"],
                    }
                ]
            },
        )
        response.raise_for_status()

    async def get(self, learner_token: str) -> list[Note]:
        """Return every app's notes for the token's learner."""
        base, headers = self._route(learner_token)
        response = await self._http.get(
            f"{base}/memories/list", headers=headers, params={"limit": 100}
        )
        if response.status_code == httpx.codes.NOT_FOUND:
            return []
        response.raise_for_status()
        return [_note(item) for item in response.json()["items"]]

    async def search(self, learner_token: str, query: str) -> list[Note]:
        """Return the learner's notes most relevant to ``query``."""
        base, headers = self._route(learner_token)
        response = await self._http.post(
            f"{base}/memories/recall", headers=headers, json={"query": query}
        )
        if response.status_code == httpx.codes.NOT_FOUND:
            return []
        response.raise_for_status()
        return [
            _note(item, item["scores"]["final"]) for item in response.json()["results"]
        ]

    async def delete(self, learner_token: str, key: str) -> None:
        """Delete the app's note ``key`` for the token's learner."""
        base, headers = self._route(learner_token)
        response = await self._http.delete(
            f"{base}/documents/{self.app}.{key}", headers=headers
        )
        if response.status_code != httpx.codes.NOT_FOUND:
            response.raise_for_status()


class ForgetClient:
    """The platform's forget path, run with the admin client's token.

    :param base_url: Hindsight API URL.
    :param admin_token: Client-credentials token of the platform admin client.
    """

    def __init__(self, base_url: str, admin_token: str) -> None:
        """Create the client."""
        self._http = httpx.AsyncClient(
            base_url=base_url,
            headers={"authorization": f"Bearer {admin_token}"},
            timeout=HTTP_TIMEOUT_SECONDS,
        )

    async def aclose(self) -> None:
        """Close the underlying HTTP client."""
        await self._http.aclose()

    async def forget_user(self, learner_id: str) -> int:
        """Delete everything for one learner, durably.

        Two phases, because a bank delete cascades away the bank's operation rows
        and with them the only way to see work still in flight:

        1. Clear the bank's memories. This raises the server-side fence (no new
           write, bank creation or worker retry is accepted) but keeps the bank
           row, so its operations stay listable. Cancel what is pending and wait
           for what is processing to finish.
        2. Delete the bank. Nothing can be writing by now, so this removes the
           remainder.

        The whole sequence is idempotent, so a transient failure (a deadlock
        with the retain being forgotten, a timeout, work still in flight past the
        drain deadline) is retried from the start rather than left half done. A
        caller that still gets an error has to retry later: until the bank is
        deleted, the fence is all that keeps the learner's data from coming back.

        :param learner_id: The learner's Keycloak user id.
        :returns: Rows removed by the attempt that completed.
        :rtype: int
        """
        for attempt in range(1, FORGET_ATTEMPTS):
            try:
                return await self._forget(learner_id)
            except (httpx.TransportError, httpx.HTTPStatusError, TimeoutError) as exc:
                if isinstance(exc, httpx.HTTPStatusError) and (
                    exc.response.status_code < httpx.codes.INTERNAL_SERVER_ERROR
                ):
                    raise
                await asyncio.sleep(FORGET_BACKOFF_SECONDS * 2 ** (attempt - 1))
        return await self._forget(learner_id)

    async def _forget(self, learner_id: str) -> int:
        base = f"/v1/default/banks/{bank_for(learner_id)}"
        headers = {"x-ol-learner": learner_id}
        removed = 0

        response = await self._http.delete(f"{base}/memories", headers=headers)
        if response.status_code != httpx.codes.NOT_FOUND:
            response.raise_for_status()
            removed += response.json()["deleted_count"]
            await self._drain(base, headers)

        response = await self._http.delete(base, headers=headers)
        # An earlier attempt may have deleted the bank before its response was
        # lost.
        if response.status_code == httpx.codes.NOT_FOUND:
            return removed
        response.raise_for_status()
        return removed + response.json()["deleted_count"]

    async def _drain(self, base: str, headers: dict[str, str]) -> None:
        deadline = time.monotonic() + DRAIN_TIMEOUT_SECONDS
        while True:
            pending = await self._ops(base, headers, "pending")
            for op in pending:
                response = await self._http.delete(
                    f"{base}/operations/{op['id']}", headers=headers
                )
                # 409: a worker claimed it between the listing and the cancel.
                # It is processing now, and the loop waits for it like any other.
                if response.status_code != httpx.codes.CONFLICT:
                    response.raise_for_status()
            if not pending and not await self._ops(base, headers, "processing"):
                return
            if time.monotonic() > deadline:
                msg = f"{base} still had work in flight after {DRAIN_TIMEOUT_SECONDS}s"
                raise TimeoutError(msg)
            await asyncio.sleep(DRAIN_POLL_SECONDS)

    async def _ops(
        self, base: str, headers: dict[str, str], status: str
    ) -> list[dict[str, str]]:
        response = await self._http.get(
            f"{base}/operations",
            headers=headers,
            params={"status": status, "limit": OPERATIONS_PAGE_SIZE},
        )
        response.raise_for_status()
        return response.json()["operations"]
