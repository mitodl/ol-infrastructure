"""Resolve commit-author identities to Slack mentions.

The release resource writes each checklist item as ``- [ ] <item> by
<author>``, where ``<author>`` is the commit author's *email* (see
``_parse_commit_log`` in ol-concourse's release resource). Posting those
strings straight into Slack notifies nobody and puts addresses in a channel,
so every author is resolved to a real Slack user with ``users.lookupByEmail``
and rendered as ``<@U…>``.

That lookup needs the ``users:read.email`` scope on the bot token. Without it
Slack returns ``missing_scope``; lookups are then disabled for the life of the
process (one loud log line rather than an API call per author per poll) and
every author falls back to a plain-text handle.

Doof did this with a difflib fuzzy match of the git author *name* against
every Slack user's ``real_name`` at a 0.8 threshold, because the release PR
gave it no emails. Emails make an exact lookup possible, so the fuzzy match is
not carried over.
"""

import logging
import re
import time
from collections.abc import Iterable
from typing import Any

log = logging.getLogger(__name__)

# A resolved Slack id effectively never changes, so cache it for a day. A
# *failed* lookup expires much sooner: someone who joins Slack, or whose git
# email is added to their profile, should start getting pinged without waiting
# for a pod restart.
_HIT_TTL_SECONDS = 24 * 60 * 60
_MISS_TTL_SECONDS = 15 * 60

# author string -> (rendered mention or fallback, expires_at)
_cache: dict[str, tuple[str, float]] = {}

# Set when Slack reports the token lacks users:read.email. Every lookup would
# fail the same way, and the checkbox watcher polls every 60s per author.
_lookups_disabled = False

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+$")
# GitHub's no-reply addresses carry the account handle: both the legacy
# "handle@users.noreply.github.com" and the current
# "12345+handle@users.noreply.github.com".
_GITHUB_NOREPLY_RE = re.compile(
    r"^(?:\d+\+)?(?P<handle>[^@\s]+)@users\.noreply\.github\.com$", re.IGNORECASE
)
# Commit authors that are machines. The github-issues resource's
# `auto_check_authors` normally checks their boxes off before anyone sees the
# issue, so these should not reach a lookup -- but a repo that hasn't
# configured it would otherwise spend an API call per poll failing to find a
# Slack account for renovate.
_BOT_AUTHOR_RE = re.compile(
    r"(\[bot\]|^(renovate|dependabot|github-actions|noreply)@)", re.IGNORECASE
)


def _fallback(author: str) -> str:
    """Render an author we can't mention, without leaking their address.

    A GitHub handle is more use to a human than an address, and the address
    is what Doof never put in the channel in the first place.
    """
    noreply = _GITHUB_NOREPLY_RE.match(author)
    if noreply:
        return f"`{noreply.group('handle')}`"
    # Truncate at the first "@" whatever the rest looks like: a git author
    # field is an arbitrary string, and something address-shaped but invalid
    # ("alice@@example.com") must not post itself in full just because it
    # fails _EMAIL_RE. That pattern gates API lookups, not redaction.
    local_part = author.split("@", 1)[0]
    return f"`{local_part or 'unknown author'}`"


def _error_code(exc: Exception) -> str:
    """Return Slack's error string from a SlackApiError, or "" for anything else.

    Read reflectively so this module doesn't import slack_sdk: it is exercised
    in a test environment that has neither slack_sdk nor slack_bolt installed.
    """
    response = getattr(exc, "response", None)
    if response is None:
        return ""
    try:
        return str(response["error"])
    except Exception:  # noqa: BLE001
        return ""


def _cached(author: str) -> str | None:
    entry = _cache.get(author)
    if entry is None:
        return None
    rendered, expires_at = entry
    if expires_at <= time.monotonic():
        del _cache[author]
        return None
    return rendered


def _store(author: str, rendered: str, ttl: float) -> str:
    _cache[author] = (rendered, time.monotonic() + ttl)
    return rendered


async def mention(client: Any, author: str) -> str:
    """Return ``<@U…>`` for *author*, or a plain-text fallback."""
    global _lookups_disabled  # noqa: PLW0603

    cached = _cached(author)
    if cached is not None:
        return cached
    if _lookups_disabled or not _EMAIL_RE.match(author):
        return _fallback(author)
    if _BOT_AUTHOR_RE.search(author):
        return _store(author, _fallback(author), _HIT_TTL_SECONDS)

    try:
        response = await client.users_lookupByEmail(email=author)
    except Exception as exc:  # noqa: BLE001
        code = _error_code(exc)
        if code == "missing_scope":
            _lookups_disabled = True
            log.error(  # noqa: TRY400
                "Slack rejected users.lookupByEmail for lack of the "
                "users:read.email scope; release notifications will name "
                "authors without @-mentioning them until the bot token is "
                "reinstalled with that scope"
            )
        elif code != "users_not_found":
            log.warning(
                "Slack lookup for %s failed: %s", _fallback(author), code or exc
            )
        return _store(author, _fallback(author), _MISS_TTL_SECONDS)

    user = (
        (response.get("user") or {}) if hasattr(response, "get") else response["user"]
    )
    user_id = user.get("id")
    if not user_id or user.get("deleted"):
        # A deactivated account can still be looked up, and mentioning one
        # notifies nobody while reading as though someone was pinged.
        return _store(author, _fallback(author), _HIT_TTL_SECONDS)
    return _store(author, f"<@{user_id}>", _HIT_TTL_SECONDS)


async def format_authors(client: Any, authors: Iterable[str]) -> str:
    """Render a set of commit authors as a comma-separated Slack mention list."""
    return ", ".join([await mention(client, author) for author in sorted(authors)])
