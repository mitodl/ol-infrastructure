"""Resolve configured Slack channel names to the IDs chat.postMessage needs.

``AppRegistration.slack_channel`` (src/bridge/settings/apps.py) and
``RELEASE_ANNOUNCE_CHANNEL`` are written as human-readable names --
"product-mit-learn", "product-infrastructure". Slack's docs are explicit that
``chat.postMessage`` wants "an encoded ID" and to "always use channel-like IDs
instead to make sure your message gets to where it's going"; a bare name gets
``channel_not_found``. That is what silently broke every proactive message the
bot sends -- ready-to-promote, checkbox progress, deploy milestones, stuck
release nags -- while slash-command replies kept working, because those go
through Bolt's ``respond`` URL rather than the Web API.

Names are resolved here through ``conversations.list`` and cached. Both public
and private channels are requested, which needs ``channels:read`` and
``groups:read`` respectively. Slack's docs do not say whether asking for a type
the token cannot see fails the whole call or just omits that type, so this does
not depend on the answer: a ``missing_scope`` response narrows the request to
private channels (what the release channels actually are) and retries once.

Slack lists private channels only for the workspaces and conversations the
token can reach, so a name that does not resolve means one of two things worth
saying out loud: the bot is not in that channel, or the name is wrong.
"""

import logging
import os
import re
import time
from typing import Any

log = logging.getLogger(__name__)

# Slack conversation ids: C (public), G (legacy private group), D (DM).
_ID_RE = re.compile(r"^[CGD][A-Z0-9]{7,}$")

_PRIVATE_ONLY = "private_channel"
_ALL_CHANNEL_TYPES = f"public_channel,{_PRIVATE_ONLY}"

# Pinning this in the environment turns off the narrowing retry below: an
# explicit setting is a deliberate choice, not something to second-guess.
_TYPES_PINNED = bool(os.environ.get("SLACK_CHANNEL_TYPES"))
_channel_types = os.environ.get("SLACK_CHANNEL_TYPES", _ALL_CHANNEL_TYPES)

_PAGE_SIZE = 200

# Re-listing on every cache miss would mean one conversations.list per poll
# for a name that is simply wrong or a channel the bot was never invited to.
_REFRESH_INTERVAL_SECONDS = 15 * 60

# lowercased channel name -> id
_name_to_id: dict[str, str] = {}
_last_refresh: float | None = None
# Whether the most recent listing attempt actually succeeded. A failed one
# leaves every name unresolved, which must not be reported as "these channels
# are misconfigured" -- see `unresolvable`.
_last_refresh_ok = False

# Set when Slack reports the token cannot list conversations at all. Every
# later call would fail the same way, and these run on 60s/120s polls.
_listing_disabled = False


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


def _page_channels(response: Any) -> tuple[list[dict[str, Any]], str]:
    """Return (channels, next_cursor) from one conversations.list page."""
    channels = response.get("channels") or []
    cursor = (response.get("response_metadata") or {}).get("next_cursor") or ""
    return channels, cursor


async def _list_all(client: Any, types: str) -> tuple[dict[str, str] | None, str]:
    """Return (name -> id, "") for *types*, or (None, slack error code)."""
    mapping: dict[str, str] = {}
    cursor = ""
    while True:
        try:
            response = await client.conversations_list(
                types=types,
                exclude_archived=True,
                limit=_PAGE_SIZE,
                cursor=cursor,
            )
        except Exception as exc:
            code = _error_code(exc)
            if code != "missing_scope":
                # A scope error is handled by the caller, which may still have
                # a narrower request to try; anything else is worth a trace.
                log.exception("Failed to list Slack conversations")
            return None, code
        channels, cursor = _page_channels(response)
        for channel in channels:
            name = channel.get("name")
            channel_id = channel.get("id")
            if name and channel_id:
                mapping[name.lower()] = channel_id
        if not cursor:
            return mapping, ""


async def _refresh(client: Any) -> bool:
    """Rebuild the name -> id map. Return whether the listing succeeded."""
    global _channel_types, _last_refresh, _last_refresh_ok, _listing_disabled  # noqa: PLW0603

    while True:
        mapping, code = await _list_all(client, _channel_types)
        if mapping is not None:
            # Replace wholesale so a renamed or archived channel drops out.
            _name_to_id.clear()
            _name_to_id.update(mapping)
            _last_refresh = time.monotonic()
            _last_refresh_ok = True
            log.info(
                "Resolved %s Slack channel(s) from conversations.list", len(mapping)
            )
            return True

        if (
            code == "missing_scope"
            and not _TYPES_PINNED
            and _channel_types != _PRIVATE_ONLY
        ):
            # The token cannot see every requested type. Which types a partial
            # token gets back is undocumented, so drop to the one the release
            # channels actually need instead of assuming either behaviour.
            log.warning(
                "Slack rejected conversations.list for %s; retrying with %s."
                " Grant channels:read as well if any release channel is public",
                _channel_types,
                _PRIVATE_ONLY,
            )
            _channel_types = _PRIVATE_ONLY
            continue

        if code == "missing_scope":
            _listing_disabled = True
            log.error(
                "Slack rejected conversations.list for lack of scope; the bot"
                " cannot turn configured channel names into ids and every"
                " proactive message will fail with channel_not_found."
                " Grant groups:read (private channels) and channels:read"
                " (public) on the bot token, or configure channel ids directly"
            )
        # Keep whatever the last successful refresh cached rather than dropping
        # known-good ids because one refresh failed.
        _last_refresh = time.monotonic()
        _last_refresh_ok = False
        return False


def _stale() -> bool:
    return (
        _last_refresh is None
        or time.monotonic() - _last_refresh >= _REFRESH_INTERVAL_SECONDS
    )


async def resolve(client: Any, channel: str) -> str:
    """Return the channel id for *channel*, or the input unchanged.

    Returning the input on failure keeps the caller's own error handling in
    play: the post then fails with the same ``channel_not_found`` it would
    have anyway, next to this module's log line saying why.
    """
    if _ID_RE.match(channel):
        return channel
    name = channel.lstrip("#").lower()

    cached = _name_to_id.get(name)
    if cached:
        return cached
    if _listing_disabled:
        return channel
    if _stale():
        await _refresh(client)
        cached = _name_to_id.get(name)
        if cached:
            return cached

    log.warning(
        "No Slack channel named %r is visible to the bot. Private channels are"
        " only listed once the bot is a member, so either invite it to the"
        " channel or correct the configured name",
        name,
    )
    return channel


class ListingError(RuntimeError):
    """conversations.list did not succeed, so nothing can be classified."""


async def unresolvable(client: Any, channels: set[str]) -> list[str]:
    """Return the configured *channels* that cannot be turned into an id.

    Called once at startup so a misconfigured or un-invited channel is visible
    at boot, rather than as a failed post buried in a poll loop later.

    Raises ``ListingError`` when the listing itself did not succeed. Without
    that, a Slack outage or a rate limit at boot leaves every name unresolved
    and would be reported as "all eight channels are misconfigured" -- sending
    someone to check invites that are perfectly fine.
    """
    if _stale() or not _last_refresh_ok:
        await _refresh(client)
    if not _last_refresh_ok:
        msg = "conversations.list did not succeed; channel state is unknown"
        raise ListingError(msg)
    return [
        channel
        for channel in sorted(channels)
        if await resolve(client, channel) == channel and not _ID_RE.match(channel)
    ]
