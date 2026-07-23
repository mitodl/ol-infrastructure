"""Make release_bot's flat-file modules importable by name for its tests.

The release_bot app is a standalone containerized service whose modules
(github_client, concourse_client, bot_config, bot) use bare imports (e.g.
`import github_client as github`) rather than the ol_infrastructure package
namespace, matching how they're laid out inside the container image.
"""

import sys
from pathlib import Path

_RELEASE_BOT_SRC = (
    Path(__file__).parents[4]
    / "src"
    / "ol_infrastructure"
    / "applications"
    / "release_bot"
)
if str(_RELEASE_BOT_SRC) not in sys.path:
    sys.path.insert(0, str(_RELEASE_BOT_SRC))
