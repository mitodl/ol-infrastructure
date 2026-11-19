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

AN EMAIL LOOKUP ONLY WORKS IF THE COMMIT EMAIL *IS* THE SLACK EMAIL, and for
much of the team it is not. The mitxonline 2026.10.28.1 RC announcement read
"Still waiting on: `Anas12091101`, `dsubak`, @James Kachel": only James commits
under his Slack address. The other two commit as
``dsubak@users.noreply.github.com`` and
``88967643+Anas12091101@users.noreply.github.com``, addresses no Slack account
can ever carry, so the two people who needed the ping were exactly the two who
did not get one. ``SLACK_USER_OVERRIDES`` (``configure_overrides`` below) maps
such identities to a Slack member id explicitly. It is consulted *before* the
email lookup, and independently of it, so overrides still resolve on a token
that lacks ``users:read.email`` altogether.

Doof did this with a difflib fuzzy match of the git author *name* against
every Slack user's ``real_name`` at a 0.8 threshold, because the release PR
gave it no emails. That was not reliably correct and is not carried over: an
override is an exact hand-maintained entry, never an inferred one.
"""

import logging
import re
import time
from collections.abc import Iterable, Mapping
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

# Identity -> Slack member id, from SLACK_USER_OVERRIDES. Keys are lowercased;
# _identity_keys says what an author is matched on.
_overrides: dict[str, str] = {}

# Slack member ids: U for an ordinary account, W for an Enterprise Grid one
# (much of this workspace is on W ids, so matching only U would reject half the
# team). The {7,} bound is the one _ID_RE uses in slack_channels, and is a
# floor rather than an exact length: it rejects an obviously truncated paste
# without betting on how long Slack keeps making ids.
# Validated at configure time because a *wrong* id fails silently -- Slack
# renders an unresolvable `<@U123>` as literal text, which is the same
# nobody-got-pinged outcome this list exists to fix.
_SLACK_USER_ID_RE = re.compile(r"^[UW][A-Z0-9]{7,}$")

_SLACK_MARKUP_RE = re.compile(r"[`<>&]")
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


def _identity_keys(author: str) -> list[str]:
    """Return the override keys *author* may be listed under, most specific first.

    An author string is whatever the release resource read out of the commit,
    so two things are worth keying on:

    * the string itself -- usually a full commit email
      (``james@jkachel.com``), or a bare name for an author field with no
      address in it at all;
    * the GitHub handle, for the no-reply addresses that are the whole reason
      this exists. Keying ``dsubak`` rather than the address covers both
      no-reply forms at once, and the legacy one carries no account id to key
      on anyway.

    Deliberately *not* keyed on the local part of an arbitrary address:
    treating ``jkachel@mit.edu`` and ``jkachel@tacostand.local`` as the same
    person is a guess, and this module does not guess. List the second address
    as well if it matters.
    """
    author = author.strip()
    keys = [author.lower()]
    noreply = _GITHUB_NOREPLY_RE.match(author)
    if noreply:
        keys.append(noreply.group("handle").lower())
    return keys


def configure_overrides(overrides: Mapping[str, str]) -> None:
    """Install the identity -> Slack member id override list.

    Called once at boot, and in tests. An entry whose value is not a member id
    is dropped with a log line rather than taken on faith, because rendering
    ``<@dsubak>`` puts a dead mention in a release channel that reads exactly
    like a successful ping. Pass ``{}`` to clear.
    """
    _overrides.clear()
    rejected = []
    for identity, user_id in overrides.items():
        # Tolerate the two shapes a human copying an id out of Slack produces:
        # a leading "@", and the lowercase of a hand-typed id.
        normalized = str(user_id).strip().lstrip("@").upper()
        if not _SLACK_USER_ID_RE.match(normalized):
            rejected.append(identity)
            continue
        _overrides[identity.strip().lower()] = normalized
    # A fallback cached before configure() ran would outlive the override by a
    # day, which is most of the way to "the fix did not ship".
    _cache.clear()
    if rejected:
        log.error(
            "Ignoring %s SLACK_USER_OVERRIDES entr(ies) whose value is not a "
            "Slack member id (expected U… or W…, not a display name, handle, "
            "or email): %s",
            len(rejected),
            ", ".join(sorted(rejected)),
        )
    log.info("Loaded %s Slack user override(s)", len(_overrides))


def _override(author: str) -> str | None:
    """Return the configured mention for *author*, or None if not listed."""
    for key in _identity_keys(author):
        user_id = _overrides.get(key)
        if user_id:
            return f"<@{user_id}>"
    return None


def _fallback(author: str) -> str:
    """Render an author we can't mention, without leaking their address.

    A GitHub handle is more use to a human than an address, and the address
    is what Doof never put in the channel in the first place.
    """
    # An author is text from a commit or an edited issue body. A backtick would
    # close the code span and let a following `<!channel>` ping the channel, so
    # drop the characters Slack treats as markup before wrapping it.
    author = _SLACK_MARKUP_RE.sub("", author)
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


async def _lookup_by_email(client: Any, author: str) -> str:
    """Resolve *author*'s commit email through Slack, and cache the outcome."""
    global _lookups_disabled  # noqa: PLW0603

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


async def mention(client: Any, author: str) -> str:
    """Return ``<@U…>`` for *author*, or a plain-text fallback."""
    cached = _cached(author)
    if cached is not None:
        return cached
    # Ahead of every other branch, including the bot filter and the
    # lookups-disabled short circuit: an override is a person stating that this
    # identity is that account, and nothing inferred here should outrank it.
    override = _override(author)
    if override is not None:
        return _store(author, override, _HIT_TTL_SECONDS)
    if _lookups_disabled or not _EMAIL_RE.match(author):
        return _fallback(author)
    if _BOT_AUTHOR_RE.search(author):
        return _store(author, _fallback(author), _HIT_TTL_SECONDS)
    return await _lookup_by_email(client, author)


async def format_authors(client: Any, authors: Iterable[str]) -> str:
    """Render a set of commit authors as a comma-separated Slack mention list."""
    return ", ".join([await mention(client, author) for author in sorted(authors)])


async def unusable_overrides(client: Any) -> list[str]:
    """Return the override identities whose Slack id cannot be mentioned.

    Shape and intent copied from ``slack_channels.unresolvable``: an override
    that points at a deleted account, or at a well-formed id belonging to no
    one, renders as a mention that notifies nobody. Nothing downstream can tell
    that apart from a working ping, so the list is checked once at boot and
    named in the log rather than discovered by someone not being nagged.

    Returns ``[]`` -- not a complaint -- if the token cannot call ``users.info``
    at all, since that says nothing about whether the ids are right.
    """
    unusable = []
    for identity, user_id in sorted(_overrides.items()):
        try:
            response = await client.users_info(user=user_id)
        except Exception as exc:  # noqa: BLE001
            code = _error_code(exc)
            if code == "missing_scope":
                log.warning(
                    "Cannot verify SLACK_USER_OVERRIDES: the bot token lacks "
                    "users:read. The overrides are still applied as written."
                )
                return []
            unusable.append(f"{identity} -> {user_id} ({code or exc})")
            continue
        user = (
            (response.get("user") or {})
            if hasattr(response, "get")
            else response["user"]
        )
        if user.get("deleted"):
            unusable.append(f"{identity} -> {user_id} (deactivated account)")
    return unusable
