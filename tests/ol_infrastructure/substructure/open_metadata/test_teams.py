"""The team sync must see the same teams om_server_config.py creates.

The sync deletes a Keycloak group whose team it is not told about, so a team
the source parse misses (e.g. one added by a later ``TEAM_CONFIG[...] =``
assignment) would lose its group and every member.
"""

import importlib
from pathlib import Path

import pytest

from ol_infrastructure.substructure.open_metadata import scripts
from ol_infrastructure.substructure.open_metadata.teams import declared_team_names

SERVER_CONFIG_SCRIPT = Path(scripts.__file__).parent / "om_server_config.py"


def test_declared_teams_match_the_imported_team_config(monkeypatch):
    # The module reads these at import time.
    monkeypatch.setenv("OM_SERVER_URL", "http://openmetadata.invalid/api")
    monkeypatch.setenv("OM_BOT_JWT_TOKEN", "not-a-token")  # pragma: allowlist secret
    om_server_config = importlib.import_module(
        "ol_infrastructure.substructure.open_metadata.scripts.om_server_config"
    )

    assert declared_team_names(SERVER_CONFIG_SCRIPT.read_text()) == sorted(
        om_server_config.TEAM_CONFIG
    )


def test_a_script_without_team_config_is_refused():
    with pytest.raises(ValueError, match="TEAM_CONFIG"):
        declared_team_names("OTHER: dict[str, str] = {}\n")
