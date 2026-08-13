"""Make release_bot's flat-file modules importable by name for its tests.

The release_bot app is a standalone containerized service whose modules
(github_client, concourse_client, bot_config, bot) use bare imports (e.g.
`import github_client as github`) rather than the ol_infrastructure package
namespace, matching how they're laid out inside the container image.
"""

import asyncio
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest


@pytest.fixture(autouse=True)
def _preserve_global_event_loop():
    """Put the process-global event loop back after every test here.

    `tests/test_example_correct_pattern.py` installs Pulumi's mocks at import
    time, which binds Pulumi's runtime state to whatever event loop is current
    during collection. pytest-asyncio runs in `asyncio_mode = "auto"` with a
    per-function loop scope, so every `async def` test in this package swaps
    the global loop out and closes its own afterwards -- leaving no current
    loop behind. Those Pulumi tests are collected from the repository root and
    run *after* this package, so they then fail with "attached to a different
    loop" / "await wasn't used with future", despite having nothing to do with
    the release bot.

    `test_github_client.py` sidesteps this by testing the sync helpers
    directly, but the Slack command handlers are async all the way down and
    have no sync core to call, so the loop has to be restored instead.
    """
    try:
        previous = asyncio.get_event_loop()
    except RuntimeError:
        previous = None
    yield
    if previous is not None and not previous.is_closed():
        asyncio.set_event_loop(previous)


_RELEASE_BOT_SRC = (
    Path(__file__).parents[4]
    / "src"
    / "ol_infrastructure"
    / "applications"
    / "release_bot"
)
if str(_RELEASE_BOT_SRC) not in sys.path:
    sys.path.insert(0, str(_RELEASE_BOT_SRC))


def _stub_slack_bolt() -> None:
    """Register minimal slack_bolt stand-ins so `bot` is importable.

    slack_bolt is a dependency of the release_bot container image
    (requirements.txt), not of ol-infrastructure itself, so it is absent from
    the repo's test environment -- which is why bot.py had no tests at all.
    Only the two names bot.py binds at import time are needed; every handler
    under test takes its Slack callables (ack/respond/client) as arguments, so
    nothing real is exercised through these.
    """
    if "slack_bolt" in sys.modules:
        return
    slack_bolt = types.ModuleType("slack_bolt")
    async_app = types.ModuleType("slack_bolt.async_app")
    async_app.AsyncApp = MagicMock(name="AsyncApp")  # type: ignore[attr-defined]
    adapter = types.ModuleType("slack_bolt.adapter")
    socket_mode = types.ModuleType("slack_bolt.adapter.socket_mode")
    async_handler = types.ModuleType("slack_bolt.adapter.socket_mode.async_handler")
    async_handler.AsyncSocketModeHandler = MagicMock(  # type: ignore[attr-defined]
        name="AsyncSocketModeHandler"
    )
    sys.modules.update(
        {
            "slack_bolt": slack_bolt,
            "slack_bolt.async_app": async_app,
            "slack_bolt.adapter": adapter,
            "slack_bolt.adapter.socket_mode": socket_mode,
            "slack_bolt.adapter.socket_mode.async_handler": async_handler,
        }
    )


_stub_slack_bolt()
