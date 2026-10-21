"""Glossary terms are create-only, and a steward's deletes and moves must stick.

om_server_config.py runs as a Job on every change to the script, and a failed
Job fails the next ``pulumi up``. A term a steward soft-deleted, or moved under
another parent, is missing at the FQN the script asks for, while OpenMetadata
still holds its name: the glossary-wide, case-insensitive duplicate check
counts deleted rows. Recreating it would get a 400 on every run. A term
declared under a moved or soft-deleted parent, or under a soft-deleted glossary,
would get a 404, because the server checks that both are live first.
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
    """GET by name from a dict of entities; POST answers with a fixed status.

    Like the server, a term POST whose glossary or parent is missing or
    soft-deleted gets a 404 before any other check.
    """

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

    def _live(self, key: str) -> bool:
        entity = self.entities.get(key)
        return entity is not None and not entity.get("deleted")

    def post(self, url: str, **kwargs: Any) -> FakeResponse:
        collection = url.split("/v1/")[1]
        body = kwargs["json"]
        self.posts.append((collection, body))
        if collection == "glossaryTerms" and not (
            self._live(f"glossaries/{body['glossary']}")
            and ("parent" not in body or self._live(f"glossaryTerms/{body['parent']}"))
        ):
            return FakeResponse(404, {"code": 404, "message": "not found"})
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


def _fqn(term: dict[str, Any]) -> str:
    parent = f"open-learning.{term['parent']}" if "parent" in term else "open-learning"
    return f"{parent}.{term['name']}"


def _glossary(
    config: ModuleType, *, deleted: bool = False, without: tuple[str, ...] = ()
) -> dict[str, dict[str, Any]]:
    """Return the glossary and its terms at their declared FQNs, minus *without*."""
    return {
        "glossaries/open-learning": {"deleted": deleted},
        **{
            f"glossaryTerms/{_fqn(term)}": {"deleted": deleted}
            for term in config.GLOSSARY_TERMS
            if term["name"] not in without
        },
    }


def _run_glossary_steps(monkeypatch: pytest.MonkeyPatch, config: ModuleType) -> None:
    for name in (
        "TAG_CONFIG",
        "TEAM_CONFIG",
        "POLICY_CONFIG",
        "ROLE_CONFIG",
        "TEAM_DEFAULT_ROLES",
        "DOMAIN_CONFIG",
    ):
        monkeypatch.setattr(config, name, {})
    config.main()


def test_missing_term_is_created_under_its_parent(
    monkeypatch: pytest.MonkeyPatch, config: ModuleType
) -> None:
    om = FakeOM(_glossary(config, without=("direct-identifier",)))
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


@pytest.mark.parametrize("name", ["identifiability", "ferpa-classification"])
def test_exclusive_parent_is_created_mutually_exclusive(
    monkeypatch: pytest.MonkeyPatch, config: ModuleType, name: str
) -> None:
    om = FakeOM(_glossary(config, without=(name,)))
    _install(monkeypatch, config, om)

    config._reconcile_glossary_term(_term(config, name))

    assert [body["mutuallyExclusive"] for _, body in om.posts] == [True]


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
    om = FakeOM(_glossary(config, without=("learner",)), post_status=400)
    _install(monkeypatch, config, om)

    config._reconcile_glossary_term(_term(config, "learner"))

    assert len(om.posts) == 1
    assert "[skip]" in capsys.readouterr().out


def test_other_create_failures_still_raise(
    monkeypatch: pytest.MonkeyPatch, config: ModuleType
) -> None:
    om = FakeOM(_glossary(config, without=("learner",)), post_status=403)
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


def test_children_of_a_moved_parent_are_left_alone(
    monkeypatch: pytest.MonkeyPatch,
    config: ModuleType,
    capsys: pytest.CaptureFixture[str],
) -> None:
    moved = ("identifiability", "direct-identifier", "indirect-identifier")
    om = FakeOM(
        {
            **_glossary(config, without=moved),
            "glossaryTerms/open-learning.pii.identifiability": {},
            "glossaryTerms/open-learning.pii.identifiability.direct-identifier": {},
            "glossaryTerms/open-learning.pii.identifiability.indirect-identifier": {},
        },
        post_status=400,
    )
    _install(monkeypatch, config, om)

    _run_glossary_steps(monkeypatch, config)

    assert [body["name"] for _, body in om.posts] == ["identifiability"]
    assert capsys.readouterr().out.count("[skip]") == len(moved)


def test_missing_term_in_a_soft_deleted_glossary_is_left_alone(
    monkeypatch: pytest.MonkeyPatch,
    config: ModuleType,
    capsys: pytest.CaptureFixture[str],
) -> None:
    om = FakeOM(_glossary(config, deleted=True, without=("verified",)))
    _install(monkeypatch, config, om)

    _run_glossary_steps(monkeypatch, config)

    assert om.posts == []
    assert "[skip] glossary term open-learning.verified" in capsys.readouterr().out


def test_term_parents_are_declared_before_their_children(config: ModuleType) -> None:
    seen: set[str] = set()
    for term in config.GLOSSARY_TERMS:
        if parent := term.get("parent"):
            assert parent in seen, term["name"]
        seen.add(term["name"])
