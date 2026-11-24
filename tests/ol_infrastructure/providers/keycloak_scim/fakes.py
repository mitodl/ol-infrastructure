"""Test doubles shared by the scim-for-keycloak provider tests."""

from typing import Any


class FakeResponse:
    """The parts of requests.Response the providers read."""

    def __init__(self, payload: Any, status_code: int = 200) -> None:
        self.payload = payload
        self.status_code = status_code
        self.ok = status_code < 400
        self.text = str(payload)

    def json(self) -> Any:
        return self.payload
