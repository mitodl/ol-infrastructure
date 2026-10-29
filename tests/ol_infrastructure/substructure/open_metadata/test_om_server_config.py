"""Glossary terms follow the code, and a steward's deletes and moves must stick.

om_server_config.py runs as a Job on every change to the script, and a failed
Job fails the next ``pulumi up``. An existing term whose description, synonyms,
references or related terms differ from GLOSSARY_TERMS is patched back, and
only in the fields that differ: every patch puts the term back In Review.

A term a steward soft-deleted, or moved under another parent, is missing at the
FQN the script asks for, while OpenMetadata still holds its name: the
glossary-wide, case-insensitive duplicate check counts deleted rows. Recreating
it would get a 400 on every run. A term declared under a moved or soft-deleted
parent, or under a soft-deleted glossary, would get a 404, because the server
checks that both are live first.
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

    Like the server, GET returns relatedTerms only when the fields ask for it.

    PATCHes are recorded by entity id and always succeed.

    Like the server, a term POST whose glossary or parent is missing or
    soft-deleted gets a 404 before any other check.
    """

    def __init__(
        self, entities: dict[str, dict[str, Any]], post_status: int = 201
    ) -> None:
        self.entities = entities
        self.post_status = post_status
        self.posts: list[tuple[str, dict[str, Any]]] = []
        self.patches: list[tuple[str, list[dict[str, Any]]]] = []

    def get(self, url: str, **kwargs: Any) -> FakeResponse:
        collection, name = url.split("/v1/")[1].split("/name/")
        entity = self.entities.get(f"{collection}/{name}")
        params = kwargs.get("params") or {}
        if entity is None or (entity.get("deleted") and params.get("include") != "all"):
            return FakeResponse(404)
        if "relatedTerms" not in params.get("fields", ""):
            entity = {k: v for k, v in entity.items() if k != "relatedTerms"}
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

    def patch(self, url: str, **kwargs: Any) -> FakeResponse:
        self.patches.append((url.split("/v1/")[1], kwargs["json"]))
        return FakeResponse(200, {})


@pytest.fixture
def config(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    monkeypatch.setenv("OM_SERVER_URL", "http://om/api")
    monkeypatch.setenv("OM_BOT_JWT_TOKEN", "token")
    sys.modules.pop(MODULE, None)
    return importlib.import_module(MODULE)


def _install(monkeypatch: pytest.MonkeyPatch, config: ModuleType, om: FakeOM) -> None:
    monkeypatch.setattr(config.requests, "get", om.get)
    monkeypatch.setattr(config.requests, "post", om.post)
    monkeypatch.setattr(config.requests, "patch", om.patch)


def _term(config: ModuleType, name: str) -> dict[str, Any]:
    return next(term for term in config.GLOSSARY_TERMS if term["name"] == name)


def _fqn(term: dict[str, Any]) -> str:
    parent = f"open-learning.{term['parent']}" if "parent" in term else "open-learning"
    return f"{parent}.{term['name']}"


def _stored_term(term: dict[str, Any], *, deleted: bool = False) -> dict[str, Any]:
    """Return *term* as the server stores it, matching its declaration.

    The server leaves empty lists out, and its id is the FQN here.
    """
    fqn = _fqn(term)
    stored: dict[str, Any] = {
        "id": fqn,
        "fullyQualifiedName": fqn,
        "deleted": deleted,
        "description": term["description"],
        "displayName": term["displayName"],
    }
    for field in ("synonyms", "references"):
        if term.get(field):
            stored[field] = term[field]
    return stored


def _glossary(
    config: ModuleType, *, deleted: bool = False, without: tuple[str, ...] = ()
) -> dict[str, dict[str, Any]]:
    """Return the glossary and its terms at their declared FQNs, minus *without*."""
    return {
        "glossaries/open-learning": {
            "id": "open-learning",
            "deleted": deleted,
            **config.GLOSSARY_CONFIG,
        },
        **{
            f"glossaryTerms/{_fqn(term)}": _stored_term(term, deleted=deleted)
            for term in config.GLOSSARY_TERMS
            if term["name"] not in without
        },
    }


def _link(fqn: str, relation_type: str = "relatedTo") -> dict[str, Any]:
    """Return a related-term link as the server returns it."""
    return {
        "relationType": relation_type,
        "term": {"id": fqn, "type": "glossaryTerm", "fullyQualifiedName": fqn},
    }


RELATED_TERMS: list[dict[str, Any]] = [
    {"name": "a", "displayName": "A", "description": "Term a."},
    {"name": "b", "displayName": "B", "description": "Term b.", "relatedTerms": ["a"]},
    {"name": "c", "displayName": "C", "description": "Term c.", "relatedTerms": ["a"]},
]


def _run_glossary_steps(monkeypatch: pytest.MonkeyPatch, config: ModuleType) -> None:
    for name in (
        "TAG_CONFIG",
        "DOMAIN_CONFIG",
        "CUSTOM_PROPERTY_CONFIG",
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
    assert om.patches == []
    assert "[skip]" in capsys.readouterr().out


def test_soft_deleted_term_with_a_stale_description_is_left_alone(
    monkeypatch: pytest.MonkeyPatch,
    config: ModuleType,
    capsys: pytest.CaptureFixture[str],
) -> None:
    learner = _term(config, "learner")
    om = FakeOM(
        {
            "glossaryTerms/open-learning.learner": {
                **_stored_term(learner, deleted=True),
                "description": "Edited in the UI.",
            }
        }
    )
    _install(monkeypatch, config, om)

    config._reconcile_glossary_term(learner)

    assert om.posts == []
    assert om.patches == []
    assert "[skip]" in capsys.readouterr().out


def test_term_with_a_changed_description_patches_only_the_description(
    monkeypatch: pytest.MonkeyPatch, config: ModuleType
) -> None:
    learner = _term(config, "learner")
    entities = _glossary(config)
    entities["glossaryTerms/open-learning.learner"]["description"] = "Edited in the UI."
    om = FakeOM(entities)
    _install(monkeypatch, config, om)

    config._reconcile_glossary_term(learner)

    assert om.patches == [
        (
            "glossaryTerms/open-learning.learner",
            [{"op": "add", "path": "/description", "value": learner["description"]}],
        )
    ]


@pytest.mark.parametrize(
    ("field", "stored"),
    [
        ("synonyms", ["student"]),
        ("references", []),
    ],
)
def test_term_with_changed_list_fields_is_patched(
    monkeypatch: pytest.MonkeyPatch, config: ModuleType, field: str, stored: Any
) -> None:
    pii = _term(config, "pii")
    entities = _glossary(config)
    entities["glossaryTerms/open-learning.pii"][field] = stored
    om = FakeOM(entities)
    _install(monkeypatch, config, om)

    config._reconcile_glossary_term(pii)

    assert om.patches == [
        (
            "glossaryTerms/open-learning.pii",
            [{"op": "add", "path": f"/{field}", "value": pii[field]}],
        )
    ]


def test_display_name_and_mutually_exclusive_are_not_patched(
    monkeypatch: pytest.MonkeyPatch, config: ModuleType
) -> None:
    identifiability = _term(config, "identifiability")
    entities = _glossary(config)
    entities["glossaryTerms/open-learning.identifiability"].update(
        displayName="Renamed in the UI", mutuallyExclusive=False
    )
    om = FakeOM(entities)
    _install(monkeypatch, config, om)

    config._reconcile_glossary_term(identifiability)

    assert om.patches == []


def test_glossary_matching_the_code_is_not_patched(
    monkeypatch: pytest.MonkeyPatch,
    config: ModuleType,
    capsys: pytest.CaptureFixture[str],
) -> None:
    om = FakeOM(_glossary(config))
    _install(monkeypatch, config, om)

    _run_glossary_steps(monkeypatch, config)

    assert om.posts == []
    assert om.patches == []
    assert "[fail]" not in capsys.readouterr().err


def test_glossary_with_a_changed_description_is_patched(
    monkeypatch: pytest.MonkeyPatch, config: ModuleType
) -> None:
    entities = _glossary(config)
    entities["glossaries/open-learning"]["description"] = "Edited in the UI."
    om = FakeOM(entities)
    _install(monkeypatch, config, om)

    config._reconcile_glossary()

    assert om.patches == [
        (
            "glossaries/open-learning",
            [
                {
                    "op": "add",
                    "path": "/description",
                    "value": config.GLOSSARY_CONFIG["description"],
                }
            ],
        )
    ]


def _related_glossary(
    config: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    links: dict[str, list[dict[str, Any]]],
) -> FakeOM:
    """Install a glossary of RELATED_TERMS whose terms hold *links*, by name."""
    monkeypatch.setattr(config, "GLOSSARY_TERMS", RELATED_TERMS)
    entities = _glossary(config)
    for name, term_links in links.items():
        entities[f"glossaryTerms/open-learning.{name}"]["relatedTerms"] = term_links
    om = FakeOM(entities)
    _install(monkeypatch, config, om)
    return om


def test_missing_related_term_link_is_added_by_reference(
    monkeypatch: pytest.MonkeyPatch, config: ModuleType
) -> None:
    om = _related_glossary(config, monkeypatch, {"c": [_link("open-learning.a")]})

    config._reconcile_glossary_term(RELATED_TERMS[1])

    assert om.patches == [
        (
            "glossaryTerms/open-learning.b",
            [
                {
                    "op": "add",
                    "path": "/relatedTerms",
                    "value": [
                        {
                            "relationType": "relatedTo",
                            "term": {"id": "open-learning.a", "type": "glossaryTerm"},
                        }
                    ],
                }
            ],
        )
    ]


def test_undeclared_related_term_link_is_removed_and_declared_ones_kept(
    monkeypatch: pytest.MonkeyPatch, config: ModuleType
) -> None:
    declared = _link("open-learning.a", relation_type="broader")
    om = _related_glossary(
        config,
        monkeypatch,
        {"c": [declared, _link("other-glossary.added-in-ui")]},
    )

    config._reconcile_glossary_term(RELATED_TERMS[2])

    assert om.patches == [
        (
            "glossaryTerms/open-learning.c",
            [{"op": "add", "path": "/relatedTerms", "value": [declared]}],
        )
    ]


def test_links_declared_on_later_terms_are_left_to_them(
    monkeypatch: pytest.MonkeyPatch, config: ModuleType
) -> None:
    # The server returns b's and c's links to a on a as well.
    om = _related_glossary(
        config,
        monkeypatch,
        {
            "a": [_link("open-learning.b"), _link("open-learning.c")],
            "b": [_link("open-learning.a")],
            "c": [_link("open-learning.a")],
        },
    )

    _run_glossary_steps(monkeypatch, config)

    assert om.patches == []


def test_link_to_a_missing_related_term_fails(
    monkeypatch: pytest.MonkeyPatch, config: ModuleType
) -> None:
    om = _related_glossary(config, monkeypatch, {})
    del om.entities["glossaryTerms/open-learning.a"]

    with pytest.raises(ValueError, match=r"open-learning\.a is missing"):
        config._reconcile_glossary_term(RELATED_TERMS[1])


def test_missing_term_is_created_with_related_term_fqns(
    monkeypatch: pytest.MonkeyPatch, config: ModuleType
) -> None:
    om = _related_glossary(config, monkeypatch, {})
    del om.entities["glossaryTerms/open-learning.b"]

    config._reconcile_glossary_term(RELATED_TERMS[1])

    assert [body["relatedTerms"] for _, body in om.posts] == [["open-learning.a"]]


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


def test_term_parents_and_related_terms_are_declared_earlier(
    config: ModuleType,
) -> None:
    seen: set[str] = set()
    for term in config.GLOSSARY_TERMS:
        for name in [term.get("parent"), *term.get("relatedTerms", [])]:
            assert name is None or name in seen, term["name"]
        seen.add(term["name"])


def test_glossary_descriptions_survive_the_servers_sanitizer(
    config: ModuleType,
) -> None:
    # OpenMetadata runs a description containing "<" through an HTML
    # sanitizer, which drops e.g. "<platform>" and entity-encodes backticks, so
    # the stored text would never equal the declared one and every run would
    # patch it.
    descriptions = [
        config.GLOSSARY_CONFIG["description"],
        *(term["description"] for term in config.GLOSSARY_TERMS),
    ]
    assert [d for d in descriptions if "<" in d] == []
