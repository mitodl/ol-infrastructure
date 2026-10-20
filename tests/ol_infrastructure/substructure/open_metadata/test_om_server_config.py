"""Glossary terms are create-only, and a steward's deletes and moves must stick.

om_server_config.py runs as a Job on every change to the script, and a failed
Job fails the next ``pulumi up``. A term a steward soft-deleted, or moved under
another parent, is missing at the FQN the script asks for, while OpenMetadata
still holds its name: the glossary-wide, case-insensitive duplicate check
counts deleted rows. Recreating it would get a 400 on every run.
"""

import importlib
import json
import sys
from types import ModuleType
from typing import Any

import pytest
import requests

MODULE = "ol_infrastructure.substructure.open_metadata.scripts.om_server_config"
DUPLICATE = "A term with the name 'learner' already exists in 'open-learning' glossary."


class FakeResponse(requests.Response):
    def __init__(self, status_code: int, body: Any = None) -> None:
        super().__init__()
        self.status_code = status_code
        self._content = json.dumps(body).encode()


class FakeOM:
    """GET by name from a dict of entities; POST answers with a fixed status."""

    def __init__(
        self, entities: dict[str, dict[str, Any]], post_status: int = 201
    ) -> None:
        self.entities = entities
        self.post_status = post_status
        self.posts: list[tuple[str, dict[str, Any]]] = []

    def get(self, url: str, **kwargs: Any) -> FakeResponse:
        collection, name = url.split("/v1/")[1].split("/name/")
        entity = self.entities.get(f"{collection}/{name}")
        include = (kwargs.get("params") or {}).get("include")
        if entity is None or (entity.get("deleted") and include != "all"):
            return FakeResponse(404)
        return FakeResponse(200, entity)

    def post(self, url: str, **kwargs: Any) -> FakeResponse:
        self.posts.append((url.split("/v1/")[1], kwargs["json"]))
        if self.post_status == requests.codes.bad_request:
            return FakeResponse(400, {"code": 400, "message": DUPLICATE})
        return FakeResponse(self.post_status, {})


@pytest.fixture
def config(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    monkeypatch.setenv("OM_SERVER_URL", "http://om/api")
    monkeypatch.setenv("OM_BOT_JWT_TOKEN", "token")
    sys.modules.pop(MODULE, None)
    return importlib.import_module(MODULE)


def _install(monkeypatch: pytest.MonkeyPatch, config: ModuleType, om: FakeOM) -> None:
    monkeypatch.setattr(config.requests, "get", om.get)
    monkeypatch.setattr(config.requests, "post", om.post)


def _term(config: ModuleType, name: str) -> dict[str, Any]:
    return next(term for term in config.GLOSSARY_TERMS if term["name"] == name)


def test_missing_term_is_created_under_its_parent(
    monkeypatch: pytest.MonkeyPatch, config: ModuleType
) -> None:
    om = FakeOM({})
    _install(monkeypatch, config, om)

    config._reconcile_glossary_term(_term(config, "direct-identifier"))

    assert om.posts == [
        (
            "glossaryTerms",
            {
                "glossary": "open-learning",
                **{
                    k: v
                    for k, v in _term(config, "direct-identifier").items()
                    if k != "parent"
                },
                "parent": "open-learning.identifiability",
            },
        )
    ]


def test_soft_deleted_term_is_left_alone(
    monkeypatch: pytest.MonkeyPatch,
    config: ModuleType,
    capsys: pytest.CaptureFixture[str],
) -> None:
    om = FakeOM({"glossaryTerms/open-learning.learner": {"deleted": True}})
    _install(monkeypatch, config, om)

    config._reconcile_glossary_term(_term(config, "learner"))

    assert om.posts == []
    assert "[skip]" in capsys.readouterr().out


def test_moved_term_is_left_alone(
    monkeypatch: pytest.MonkeyPatch,
    config: ModuleType,
    capsys: pytest.CaptureFixture[str],
) -> None:
    om = FakeOM({}, post_status=400)
    _install(monkeypatch, config, om)

    config._reconcile_glossary_term(_term(config, "learner"))

    assert len(om.posts) == 1
    assert "[skip]" in capsys.readouterr().out


def test_other_create_failures_still_raise(
    monkeypatch: pytest.MonkeyPatch, config: ModuleType
) -> None:
    om = FakeOM({}, post_status=403)
    _install(monkeypatch, config, om)

    with pytest.raises(requests.HTTPError):
        config._reconcile_glossary_term(_term(config, "learner"))


def test_soft_deleted_glossary_is_left_alone(
    monkeypatch: pytest.MonkeyPatch,
    config: ModuleType,
    capsys: pytest.CaptureFixture[str],
) -> None:
    om = FakeOM({"glossaries/open-learning": {"deleted": True}})
    _install(monkeypatch, config, om)

    config._reconcile_glossary()

    assert om.posts == []
    assert "[skip]" in capsys.readouterr().out


def test_term_parents_are_declared_before_their_children(config: ModuleType) -> None:
    seen: set[str] = set()
    for term in config.GLOSSARY_TERMS:
        if parent := term.get("parent"):
            assert parent in seen, term["name"]
        seen.add(term["name"])
