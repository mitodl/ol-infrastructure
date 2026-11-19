"""Fakes shared by the OpenMetadata provider tests."""

import copy
from typing import Any


class FakeResponse:
    """The parts of ``requests.Response`` the providers read."""

    def __init__(
        self, method: str, url: str, status_code: int, body: Any = None
    ) -> None:
        self.status_code = status_code
        self.ok = status_code < 400
        self.url = url
        self.text = str(body)
        self.request = type("Request", (), {"method": method})()
        self._body = body

    def json(self) -> Any:
        return copy.deepcopy(self._body)
