"""Make the reporter's flat-file module importable by name for its tests.

The reporter is a standalone containerized script (its own pyproject.toml/
uv.lock, only dependency is boto3) using a bare `reporter.py` module rather
than the ol_infrastructure package namespace, matching how it's laid out
inside the container image. boto3 is already a main dependency of
ol-infrastructure itself, so no stubbing is needed the way release_bot's
conftest.py stubs slack_bolt.
"""

import sys
from pathlib import Path

_REPORTER_SRC = (
    Path(__file__).parents[4]
    / "src"
    / "ol_infrastructure"
    / "applications"
    / "vuln_scanner"
    / "reporter"
)
if str(_REPORTER_SRC) not in sys.path:
    sys.path.insert(0, str(_REPORTER_SRC))
