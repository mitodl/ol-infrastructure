"""Shared fakes for the OpenMetadata bootstrap job's tests."""

import copy
import importlib
from types import ModuleType
from typing import Any

import pytest
import requests

SERVER = "http://openmetadata:8585/api"


class FakeResponse:
    def __init__(self, status_code: int, body: dict[str, Any] | None = None) -> None:
        self.status_code = status_code
        self._body = body or {}

    def json(self) -> dict[str, Any]:
        return copy.deepcopy(self._body)

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code))


@pytest.fixture
def om_server_config(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """Reload the job's module against the fake server's URL."""
    monkeypatch.setenv("OM_SERVER_URL", SERVER)
    monkeypatch.setenv("OM_BOT_JWT_TOKEN", "token")  # pragma: allowlist secret
    module = importlib.import_module(
        "ol_infrastructure.substructure.open_metadata.scripts.om_server_config"
    )
    return importlib.reload(module)
