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

Names are resolved here through ``conversations.list`` and cached. The release
channels are private, so this needs ``groups:read`` on the bot token (public
channels would be ``channels:read`` -- see ``_CHANNEL_TYPES``), and Slack only
lists private channels the bot has been invited to. A name that does not
resolve therefore means one of exactly two things, both worth saying out loud:
the bot is not in that channel, or the name is wrong.
"""

import logging
import os
import re
import time
from typing import Any

log = logging.getLogger(__name__)

# Slack conversation ids: C (public), G (legacy private group), D (DM).
_ID_RE = re.compile(r"^[CGD][A-Z0-9]{7,}$")

# The release channels are private, so the default asks only for what
# `groups:read` grants. Requesting public_channel as well would need
# `channels:read` too, and Slack rejects the whole call for a type the token
# cannot see -- which would take out private resolution along with it.
_CHANNEL_TYPES = os.environ.get("SLACK_CHANNEL_TYPES", "private_channel")

_PAGE_SIZE = 200

# Re-listing on every cache miss would mean one conversations.list per poll
# for a name that is simply wrong or a channel the bot was never invited to.
_REFRESH_INTERVAL_SECONDS = 15 * 60

# lowercased channel name -> id
_name_to_id: dict[str, str] = {}
_last_refresh: float | None = None

# Set when Slack reports the token cannot list conversations. Every later call
# would fail the same way, and these run on 60s/120s polls.
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


async def _refresh(client: Any) -> None:
    """Rebuild the name -> id map from conversations.list."""
    global _last_refresh, _listing_disabled  # noqa: PLW0603

    mapping: dict[str, str] = {}
    cursor = ""
    while True:
        try:
            response = await client.conversations_list(
                types=_CHANNEL_TYPES,
                exclude_archived=True,
                limit=_PAGE_SIZE,
                cursor=cursor,
            )
        except Exception as exc:
            code = _error_code(exc)
            if code == "missing_scope":
                _listing_disabled = True
                log.error(  # noqa: TRY400
                    "Slack rejected conversations.list for lack of scope; the bot"
                    " cannot turn configured channel names into ids and every"
                    " proactive message will fail with channel_not_found."
                    " Grant groups:read (private channels) on the bot token,"
                    " or configure channel ids directly"
                )
            else:
                log.exception("Failed to list Slack conversations")
            # Keep whatever the last successful refresh cached rather than
            # dropping known-good ids because one refresh failed.
            _last_refresh = time.monotonic()
            return
        channels, cursor = _page_channels(response)
        for channel in channels:
            name = channel.get("name")
            channel_id = channel.get("id")
            if name and channel_id:
                mapping[name.lower()] = channel_id
        if not cursor:
            break

    _name_to_id.clear()
    _name_to_id.update(mapping)
    _last_refresh = time.monotonic()
    log.info("Resolved %s Slack channel(s) from conversations.list", len(mapping))


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


async def unresolvable(client: Any, channels: set[str]) -> list[str]:
    """Return the configured *channels* that cannot be turned into an id.

    Called once at startup so a misconfigured or un-invited channel is visible
    at boot, rather than as a failed post buried in a poll loop later.
    """
    return [
        channel
        for channel in sorted(channels)
        if await resolve(client, channel) == channel and not _ID_RE.match(channel)
    ]
