"""Tests for the Gravitino grant reconciler.

The fake server below follows what Gravitino 1.3.1 does on the wire where the
reconciler depends on it: enum values come back in lower case, "already exists"
is a 409 with code 1004, an override names only objects that exist (404
otherwise) and replaces everything the role holds, and an object with no owner
has no ``owner`` key.

What matters is convergence in both directions (a privilege removed from the
desired state, or granted by hand, is gone after a run), that roles reach
principals only through the group of the same name, and that unsafe desired
state and unusable principals are refused.
"""

import copy
import re
from typing import Any

import pytest

from ol_infrastructure.applications.gravitino.reconcile import render_desired_state
from ol_infrastructure.applications.gravitino.scripts.reconcile_gravitino_grants import (  # noqa: E501
    Gravitino,
    ReconcileError,
    governance_role_holders,
    reconcile,
    usable_principal,
    validate_desired_roles,
)

METALAKE = "ol_data_platform"
CATALOG = "ol_data_lake_qa"
ADMIN = "service-account-ol-gravitino-admin"
LAYER_SCHEMAS = [
    f"ol_warehouse_qa_{layer}"
    for layer in (
        "raw",
        "staging",
        "intermediate",
        "dimensional",
        "mart",
        "reporting",
        "integrations",
        "external",
    )
]
MART = f"{CATALOG}.ol_warehouse_qa_mart"


def _error(status: int, code: int, kind: str) -> tuple[int, dict[str, Any]]:
    return status, {"code": code, "type": kind, "message": kind}


class FakeGravitino:
    """An in-memory stand-in for the management API."""

    def __init__(self, backend_schemas: list[str]) -> None:
        self.backend_schemas = set(backend_schemas)
        self.metalake_exists = False
        self.catalog: dict[str, Any] | None = None
        self.groups: dict[str, set[str]] = {}
        self.users: dict[str, set[str]] = {}
        self.roles: dict[str, list[dict[str, Any]]] = {}
        self.owners: dict[str, dict[str, str]] = {}
        self.writes: list[tuple[str, str]] = []

    def _object_exists(self, securable_object: dict[str, Any]) -> bool:
        if securable_object["type"].lower() == "catalog":
            return securable_object["fullName"] == CATALOG
        _, _, schema = securable_object["fullName"].partition(".")
        return schema in self.backend_schemas

    def __call__(  # noqa: C901, PLR0911, PLR0912, PLR0915
        self, method: str, path: str, body: dict[str, Any] | None
    ) -> tuple[int, dict[str, Any]]:
        if method != "GET":
            self.writes.append((method, path))
        base = f"/api/metalakes/{METALAKE}"
        if (method, path) == ("POST", "/api/metalakes"):
            if self.metalake_exists:
                return _error(409, 1004, "MetalakeAlreadyExistsException")
            self.metalake_exists = True
            self.users[ADMIN] = set()
            return 200, {"code": 0, "metalake": body}
        if not self.metalake_exists:
            return _error(403, 1008, "ForbiddenException")
        rest = path.removeprefix(base)

        if rest == f"/catalogs/{CATALOG}":
            if self.catalog is None:
                return _error(404, 1003, "NoSuchCatalogException")
            if method == "PUT":
                assert body is not None
                for update in body["updates"]:
                    if update["@type"] == "removeProperty":
                        del self.catalog["properties"][update["property"]]
                    elif not update["value"].strip():
                        return _error(400, 1001, "IllegalArgumentException")
                    else:
                        self.catalog["properties"][update["property"]] = update["value"]
            shown = copy.deepcopy(self.catalog)
            shown["properties"]["in-use"] = "true"
            return 200, {"code": 0, "catalog": shown}
        if (method, rest) == ("POST", "/catalogs"):
            self.catalog = copy.deepcopy(body)
            return 200, {"code": 0, "catalog": body}
        if rest == f"/catalogs/{CATALOG}/schemas":
            return 200, {
                "code": 0,
                "identifiers": [
                    {"namespace": [METALAKE, CATALOG], "name": schema}
                    for schema in self.backend_schemas
                ],
            }
        if match := re.fullmatch(rf"/catalogs/{CATALOG}/schemas/(.+)", rest):
            if match[1] not in self.backend_schemas:
                return _error(404, 1003, "NoSuchSchemaException")
            return 200, {"code": 0, "schema": {"name": match[1]}}

        for kind, store in (("groups", self.groups), ("users", self.users)):
            if (method, rest) == ("POST", f"/{kind}"):
                assert body is not None
                if body["name"] in store:
                    return _error(409, 1004, "AlreadyExistsException")
                store[body["name"]] = set()
                return 200, {"code": 0}
            if rest == f"/{kind}?details=true":
                return 200, {
                    "code": 0,
                    kind: [
                        {"name": name, "roles": sorted(roles)}
                        for name, roles in store.items()
                    ],
                }
            if match := re.fullmatch(rf"/permissions/{kind}/(.+)/(grant|revoke)", rest):
                assert body is not None
                names = set(body["roleNames"])
                if match[2] == "grant":
                    store[match[1]] |= names
                else:
                    store[match[1]] -= names
                return 200, {"code": 0}

        if rest == "/roles":
            if method == "GET":
                return 200, {"code": 0, "names": sorted(self.roles)}
            assert body is not None
            if body["name"] in self.roles:
                return _error(409, 1004, "RoleAlreadyExistsException")
            self.roles[body["name"]] = []
            return 200, {"code": 0}
        if match := re.fullmatch(r"/roles/(.+)", rest):
            return 200, {
                "code": 0,
                "role": {
                    "name": match[1],
                    "securableObjects": [
                        {
                            "fullName": securable_object["fullName"],
                            "type": securable_object["type"].lower(),
                            "privileges": [
                                {
                                    "name": privilege["name"].lower(),
                                    "condition": privilege["condition"].lower(),
                                }
                                for privilege in securable_object["privileges"]
                            ],
                        }
                        for securable_object in self.roles[match[1]]
                    ],
                },
            }
        if match := re.fullmatch(r"/permissions/roles/(.+)/", rest):
            assert body is not None
            for securable_object in body["overrides"]:
                if not self._object_exists(securable_object):
                    return _error(404, 1003, "NoSuchMetadataObjectException")
            self.roles[match[1]] = copy.deepcopy(body["overrides"])
            return 200, {"code": 0}

        if match := re.fullmatch(r"/owners/schema/(.+)", rest):
            if method == "PUT":
                assert body is not None
                if body["name"] not in self.groups:
                    return _error(404, 1003, "NotFoundException")
                self.owners[match[1]] = {
                    "name": body["name"],
                    "type": body["type"].lower(),
                }
                return 200, {"code": 0, "set": True}
            payload: dict[str, Any] = {"code": 0}
            if match[1] in self.owners:
                payload["owner"] = self.owners[match[1]]
            return 200, payload

        return _error(500, 1002, f"unhandled {method} {path}")

    def privileges(self, role: str, full_name: str) -> set[str]:
        for securable_object in self.roles[role]:
            if securable_object["fullName"] == full_name:
                return {
                    privilege["name"].upper()
                    for privilege in securable_object["privileges"]
                }
        return set()


def _desired() -> dict[str, Any]:
    return render_desired_state(
        env_suffix="qa",
        metalake=METALAKE,
        catalog=CATALOG,
        catalog_properties={
            "catalog-backend": "custom",
            "uri": "https://glue",
            "table-metadata-cache-impl": "",
        },
    )


def _user(username: str, saml_uid: str | None = None) -> dict[str, Any]:
    attributes = {"saml_uid": [saml_uid]} if saml_uid else {}
    return {
        "id": username,
        "username": username,
        "enabled": True,
        "attributes": attributes,
    }


def _run(
    server: FakeGravitino,
    desired: dict[str, Any] | None = None,
    holders: list[dict[str, Any]] | None = None,
) -> None:
    reconcile(
        Gravitino(server, METALAKE),
        desired or _desired(),
        lambda: holders or [],
        reconciler_principal=ADMIN,
    )


@pytest.fixture
def server() -> FakeGravitino:
    return FakeGravitino([*LAYER_SCHEMAS, "ol_warehouse_qa_tmacey_mart", "other_db"])


def test_first_run_builds_everything(server):
    _run(server, holders=[_user("alice@mit.edu", "alice")])

    assert server.catalog is not None
    assert server.catalog["provider"] == "lakehouse-iceberg"
    assert set(server.groups) == set(_desired()["roles"])
    assert server.privileges("ol_data_analyst", MART) == {"USE_SCHEMA", "SELECT_TABLE"}
    assert server.privileges("ol_data_analyst", CATALOG) == {"USE_CATALOG"}
    assert "MODIFY_TABLE" in server.privileges("ol_data_engineer", CATALOG)
    assert server.roles["ol_researcher"] == []
    assert "alice" in server.users


def test_second_run_writes_nothing_but_the_idempotent_creates(server):
    _run(server)
    server.writes.clear()
    _run(server)

    # Creates are attempted every run and answered with "already exists".
    assert {method for method, _ in server.writes} == {"POST"}
    assert not any("/permissions/" in path for _, path in server.writes)
    assert not any("/owners/" in path for _, path in server.writes)


def test_hand_made_grant_is_reverted(server):
    _run(server)
    server.roles["ol_business_analyst"].append(
        {
            "type": "schema",
            "fullName": f"{CATALOG}.ol_warehouse_qa_raw",
            "privileges": [{"name": "SELECT_TABLE", "condition": "ALLOW"}],
        }
    )
    _run(server)

    assert (
        server.privileges("ol_business_analyst", f"{CATALOG}.ol_warehouse_qa_raw")
        == set()
    )


def test_privilege_removed_from_desired_state_is_revoked(server):
    _run(server)
    desired = _desired()
    desired["roles"]["ol_data_analyst"] = [
        securable_object
        for securable_object in desired["roles"]["ol_data_analyst"]
        if securable_object["fullName"] != MART
    ]
    _run(server, desired)

    assert server.privileges("ol_data_analyst", MART) == set()
    assert server.privileges("ol_business_analyst", MART) == {
        "USE_SCHEMA",
        "SELECT_TABLE",
    }


def test_missing_schema_is_skipped_then_granted_once_it_exists(server, capsys):
    _run(server)
    migration = f"{CATALOG}.ol_warehouse_qa_migration"

    assert server.privileges("ol_data_analyst", migration) == set()
    assert "ol_warehouse_qa_migration does not exist" in capsys.readouterr().err
    assert server.privileges("ol_data_analyst", MART) == {"USE_SCHEMA", "SELECT_TABLE"}

    server.backend_schemas.add("ol_warehouse_qa_migration")
    _run(server)

    assert server.privileges("ol_data_analyst", migration) == {
        "USE_SCHEMA",
        "SELECT_TABLE",
    }


def test_roles_reach_principals_only_through_their_own_group(server):
    _run(server, holders=[_user("alice@mit.edu", "alice")])
    assert server.groups["ol_data_analyst"] == {"ol_data_analyst"}

    server.users["alice"].add("ol_data_engineer")
    server.groups["ol_researcher"].add("ol_platform_admin")
    server.groups["ol_data_analyst"].add("hand_made_role")
    _run(server, holders=[_user("alice@mit.edu", "alice")])

    assert server.users["alice"] == set()
    assert server.groups["ol_researcher"] == {"ol_researcher"}
    # Roles this does not manage are left alone.
    assert server.groups["ol_data_analyst"] == {"ol_data_analyst", "hand_made_role"}


def _retire(role: str) -> dict[str, Any]:
    desired = _desired()
    del desired["roles"][role]
    desired["retired_roles"] = [role]
    return desired


def test_retired_role_loses_its_privileges_and_every_binding(server, capsys):
    _run(server, holders=[_user("alice@mit.edu", "alice")])
    server.groups["ol_researcher"].add("ol_data_analyst")
    server.users["alice"].add("ol_data_analyst")
    capsys.readouterr()
    _run(server, _retire("ol_data_analyst"), [_user("alice@mit.edu", "alice")])

    assert server.roles["ol_data_analyst"] == []
    assert server.groups["ol_data_analyst"] == set()
    assert server.groups["ol_researcher"] == {"ol_researcher"}
    assert server.users["alice"] == set()
    assert "not managed here" not in capsys.readouterr().err
    assert server.privileges("ol_business_analyst", MART) == {
        "USE_SCHEMA",
        "SELECT_TABLE",
    }


def test_role_dropped_without_being_retired_is_only_reported(server, capsys):
    _run(server)
    desired = _desired()
    del desired["roles"]["ol_data_analyst"]
    _run(server, desired)

    assert server.groups["ol_data_analyst"] == {"ol_data_analyst"}
    assert server.privileges("ol_data_analyst", MART) == {"USE_SCHEMA", "SELECT_TABLE"}
    assert "left alone: ['ol_data_analyst']" in capsys.readouterr().err


def test_retiring_a_role_converges_and_never_creates_it(server):
    desired = _retire("ol_data_analyst")
    _run(server, desired)
    assert "ol_data_analyst" not in server.roles

    server.writes.clear()
    _run(server, desired)
    assert not any("/permissions/" in path for _, path in server.writes)


def test_role_both_managed_and_retired_is_refused_before_any_request(server):
    desired = _desired()
    desired["retired_roles"] = ["ol_data_analyst"]

    with pytest.raises(ReconcileError, match="both managed and retired"):
        _run(server, desired)
    assert server.writes == []


def test_environment_schemas_are_owned_by_the_engineering_group(server):
    _run(server)
    owner = {"name": "ol_data_engineer", "type": "group"}

    assert server.owners[MART] == owner
    assert server.owners[f"{CATALOG}.ol_warehouse_qa_tmacey_mart"] == owner
    assert f"{CATALOG}.other_db" not in server.owners

    server.owners[MART] = {"name": "alice", "type": "user"}
    _run(server)

    assert server.owners[MART] == owner


def test_catalog_property_drift_is_corrected(server):
    _run(server)
    assert server.catalog is not None
    server.catalog["properties"]["uri"] = "https://elsewhere"
    _run(server)

    assert server.catalog["properties"]["uri"] == "https://glue"


def test_immutable_catalog_property_drift_fails(server):
    _run(server)
    assert server.catalog is not None
    server.catalog["properties"]["catalog-backend"] = "jdbc"

    with pytest.raises(ReconcileError, match="immutable"):
        _run(server)


def test_users_are_added_and_never_removed(server, capsys):
    _run(server, holders=[_user("alice@mit.edu", "alice"), _user("bob@mit.edu", "bob")])
    _run(server, holders=[_user("alice@mit.edu", "alice")])

    assert {"alice", "bob", ADMIN} <= set(server.users)
    err = capsys.readouterr().err
    assert "['bob']" in err
    assert ADMIN not in err


def test_email_principal_is_refused_but_others_are_added(server, capsys):
    _run(
        server,
        holders=[
            _user("carol@mit.edu"),
            _user("alice@mit.edu", "alice"),
            _user("service-account-ol-dagster"),
        ],
    )

    assert "carol@mit.edu" not in server.users
    assert {"alice", "service-account-ol-dagster"} <= set(server.users)
    assert "Not adding carol@mit.edu" in capsys.readouterr().err


def test_every_holder_unusable_fails_the_run(server):
    with pytest.raises(ReconcileError, match="saml_uid"):
        _run(server, holders=[_user("carol@mit.edu"), _user("dave@mit.edu")])


def test_api_error_fails_the_run(server):
    def broken(method, path, body):
        if method == "GET" and path.endswith("/roles"):
            return _error(500, 1002, "RuntimeException")
        return server(method, path, body)

    with pytest.raises(ReconcileError, match="RuntimeException"):
        reconcile(Gravitino(broken, METALAKE), _desired(), list, ADMIN)


def test_not_a_metalake_member_fails_the_run():
    def forbidden(method, path, _body):
        if (method, path) == ("POST", "/api/metalakes"):
            return _error(409, 1004, "MetalakeAlreadyExistsException")
        return _error(403, 1008, "ForbiddenException")

    with pytest.raises(ReconcileError, match="ForbiddenException"):
        reconcile(Gravitino(forbidden, METALAKE), _desired(), list, ADMIN)


@pytest.mark.parametrize(
    "privilege",
    [
        "RUN_JOB",
        "CREATE_CATALOG",
        "use_job_template",
        "CREATE_ROLE",
        "MANAGE_GRANTS",
    ],
)
def test_dangerous_privileges_are_refused_before_any_request(server, privilege):
    desired = _desired()
    desired["roles"]["ol_data_engineer"][0]["privileges"].append(
        {"name": privilege, "condition": "ALLOW"}
    )

    with pytest.raises(ReconcileError, match=privilege.upper()):
        _run(server, desired)
    assert server.writes == []


def test_lone_deny_is_refused():
    def roles(denied: list[str]) -> dict[str, Any]:
        return {
            "ol_data_analyst": [
                {
                    "type": "table",
                    "fullName": f"{MART}.secret",
                    "privileges": [
                        {"name": name, "condition": "DENY"} for name in denied
                    ],
                }
            ]
        }

    validate_desired_roles(roles(["SELECT_TABLE", "MODIFY_TABLE"]))
    with pytest.raises(ReconcileError, match="denies only one"):
        validate_desired_roles(roles(["SELECT_TABLE"]))


def test_long_principal_is_unusable():
    _, problem = usable_principal(_user("service-account-" + "x" * 40))
    assert problem is not None
    assert usable_principal(_user("alice@mit.edu", "alice")) == ("alice", None)


def test_holders_are_found_through_effective_roles_and_skip_disabled_users():
    users = [
        _user("alice@mit.edu", "alice"),
        _user("bob@mit.edu", "bob"),
        {**_user("eve@mit.edu", "eve"), "enabled": False},
    ]
    effective = {"alice@mit.edu": ["ol_data_analyst"], "bob@mit.edu": ["unrelated"]}
    requested = []

    def get(url: str, _token: Any) -> Any:
        requested.append(url)
        if "/clients?clientId=ol-starrocks-client" in url:
            return [{"id": "client-uuid"}]
        if "/users?" in url:
            return users
        user_id = url.split("/users/")[1].split("/", maxsplit=1)[0]
        assert url.endswith("/role-mappings/clients/client-uuid/composite")
        return [{"name": role} for role in effective[user_id]]

    holders = governance_role_holders(
        "https://sso.example/realms/ol-data-platform",
        None,
        "ol-starrocks-client",
        {"ol_data_analyst", "ol_data_engineer"},
        get=get,
    )

    assert [user["username"] for user in holders] == ["alice@mit.edu"]
    assert requested[0].startswith(
        "https://sso.example/admin/realms/ol-data-platform/clients"
    )
    assert not any("eve@mit.edu" in url for url in requested)


def test_blank_catalog_property_is_removed_not_set(server):
    _run(server)
    assert server.catalog is not None
    server.catalog["properties"]["table-metadata-cache-impl"] = "some.Impl"
    _run(server)

    assert "table-metadata-cache-impl" not in server.catalog["properties"]


def test_keycloak_failure_does_not_hold_up_a_revocation(server):
    _run(server)
    desired = _desired()
    desired["roles"]["ol_data_analyst"] = []

    def keycloak_down() -> list[dict[str, Any]]:
        msg = "Keycloak GET returned 503"
        raise ReconcileError(msg)

    with pytest.raises(ReconcileError, match="503"):
        reconcile(Gravitino(server, METALAKE), desired, keycloak_down, ADMIN)

    assert server.roles["ol_data_analyst"] == []


def test_one_unloadable_schema_does_not_stop_the_owner_pass(server):
    server.backend_schemas.add("ol_warehouse_qa_aaa_broken")

    def flaky(method, path, body):
        if path.endswith("/schemas/ol_warehouse_qa_aaa_broken"):
            return _error(500, 1002, "RuntimeException")
        return server(method, path, body)

    with pytest.raises(ReconcileError, match="ol_warehouse_qa_aaa_broken"):
        reconcile(Gravitino(flaky, METALAKE), _desired(), list, ADMIN)

    assert server.owners[MART] == {"name": "ol_data_engineer", "type": "group"}


def test_two_users_resolving_to_one_principal_are_both_refused(server, capsys):
    _run(
        server,
        holders=[
            _user("alice@mit.edu", "shared"),
            _user("shared"),
            _user("bob@mit.edu", "bob"),
        ],
    )

    assert "shared" not in server.users
    assert "bob" in server.users
    assert "all resolve to it" in capsys.readouterr().err


def test_a_user_cannot_take_the_reconcilers_principal(server, capsys):
    _run(server, holders=[_user("mallory@mit.edu", ADMIN), _user("b@mit.edu", "bob")])

    assert "Not adding mallory@mit.edu" in capsys.readouterr().err


@pytest.mark.parametrize("object_type", ["metalake", "role"])
def test_object_types_outside_the_allow_list_are_refused(object_type):
    with pytest.raises(ReconcileError, match="object type is not allowed"):
        validate_desired_roles(
            {
                "ol_data_engineer": [
                    {
                        "type": object_type,
                        "fullName": METALAKE,
                        "privileges": [{"name": "USE_CATALOG", "condition": "ALLOW"}],
                    }
                ]
            }
        )


@pytest.mark.parametrize("saml_uid", ["alice:admin", "alice smith", "alice,bob", "a/b"])
def test_principal_outside_the_starrocks_form_is_unusable(saml_uid):
    _, problem = usable_principal(_user("alice@mit.edu", saml_uid))
    assert problem is not None


@pytest.mark.parametrize(
    ("field", "value"), [("provider", "hive"), ("type", "fileset")]
)
def test_catalog_with_the_wrong_type_or_provider_fails(server, field, value):
    _run(server)
    assert server.catalog is not None
    server.catalog[field] = value

    with pytest.raises(ReconcileError, match=f"has {field} '{value}'"):
        _run(server)


@pytest.mark.parametrize(
    "schema",
    [
        "ol_warehouse_qa_529-stage-salesforce-data_staging",
        "ol_warehouse_qa_<your name>_mart",
    ],
)
def test_schema_iceberg_cannot_name_is_skipped_without_a_request(
    server, capsys, schema
):
    server.backend_schemas.add(schema)
    requested = []

    def recording(method, path, body):
        requested.append(path)
        return server(method, path, body)

    reconcile(Gravitino(recording, METALAKE), _desired(), list, ADMIN)

    assert not any("/schemas/ol_warehouse_qa_529" in path for path in requested)
    assert not any("your" in path for path in requested)
    assert "is not a name Iceberg can load" in capsys.readouterr().err
    assert server.owners[MART] == {"name": "ol_data_engineer", "type": "group"}
