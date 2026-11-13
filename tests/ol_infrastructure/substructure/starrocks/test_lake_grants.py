"""Governance roles get exactly their data_lake_access layers on a lake catalog.

GRANTs are additive, so the revokes are what narrow a role that already holds
more, e.g. the catalog-wide SELECT the Glue catalogs granted every governance
role before they were scoped. A statement both granted and revoked would flap
on every run, so the two must never overlap.
"""

import re
import subprocess
from pathlib import Path

import pytest

from ol_infrastructure.lib.data_lake_access import (
    CATALOG_WIDE_WRITE_ROLES,
    DATA_LAKE_LAYERS,
    GOVERNANCE_LAYER_ACCESS,
    layer_database,
)
from ol_infrastructure.substructure.starrocks.lake_grants import (
    NO_SUCH_GRANT_ERROR,
    governance_grants_sql,
    layer_scoped_grants_sql,
    layer_scoped_revokes_sql,
    revoke_step,
)

CATALOG = "ol_data_lake_qa"
_PRIVILEGE = re.compile(r"(?:GRANT|REVOKE) (.+) (?:TO|FROM) ROLE (\w+);")


def _statements(sql: str, verb: str) -> list[str]:
    return [line for line in sql.splitlines() if line.startswith(verb)]


def _privileges(sql: str, verb: str) -> set[tuple[str, str]]:
    return {
        (match.group(1), match.group(2))
        for line in _statements(sql, verb)
        if (match := _PRIVILEGE.fullmatch(line))
    }


def test_grants_are_select_only_and_governance_only() -> None:
    grants = _statements(governance_grants_sql(CATALOG, "qa"), "GRANT")

    assert grants
    for grant in grants:
        assert re.match(r"GRANT (USAGE|SELECT) ON ", grant), grant
    roles = {role for _, role in _privileges("\n".join(grants), "GRANT")}
    assert roles.isdisjoint({"readonly", "batch_readonly", "app", "admin"})


def test_grants_follow_data_lake_access() -> None:
    sql = governance_grants_sql(CATALOG, "qa")

    assert (
        "GRANT SELECT ON ALL TABLES IN ALL DATABASES TO ROLE ol_data_engineer;" in sql
    )
    for layer in GOVERNANCE_LAYER_ACCESS["ol_business_analyst"]:
        assert (
            f"GRANT SELECT ON ALL TABLES IN DATABASE {layer_database('qa', layer)}"
            " TO ROLE ol_business_analyst;"
        ) in sql
    assert "ALL DATABASES TO ROLE ol_business_analyst" not in sql
    assert "ol_researcher" not in sql


def test_layer_scoped_grants_leave_out_catalog_wide_roles() -> None:
    sql = layer_scoped_grants_sql(CATALOG, "qa")

    assert "ALL DATABASES" not in sql
    for role in CATALOG_WIDE_WRITE_ROLES:
        assert role not in sql


@pytest.mark.parametrize(
    "render", [governance_grants_sql, layer_scoped_grants_sql, layer_scoped_revokes_sql]
)
def test_statements_end_in_default_catalog(render) -> None:
    assert (
        render(CATALOG, "qa")
        .splitlines()[-1]
        .startswith(("SET CATALOG default_catalog;", "REVOKE USAGE ON CATALOG"))
    )


@pytest.mark.parametrize("role", GOVERNANCE_LAYER_ACCESS)
def test_revokes_cover_everything_a_layer_scoped_role_may_not_hold(role: str) -> None:
    revoked = _privileges(layer_scoped_revokes_sql(CATALOG, "qa"), "REVOKE")
    layers = GOVERNANCE_LAYER_ACCESS[role]

    assert ("SELECT ON ALL TABLES IN ALL DATABASES", role) in revoked
    for layer in DATA_LAKE_LAYERS:
        target = (
            f"SELECT ON ALL TABLES IN DATABASE {layer_database('qa', layer)}",
            role,
        )
        assert (target in revoked) == (layer not in layers), layer
    assert ((f"USAGE ON CATALOG {CATALOG}", role) in revoked) == (not layers)


def test_revokes_never_touch_catalog_wide_roles() -> None:
    sql = layer_scoped_revokes_sql(CATALOG, "qa")

    for role in CATALOG_WIDE_WRITE_ROLES:
        assert role not in sql


def test_nothing_is_both_granted_and_revoked() -> None:
    granted = _privileges(governance_grants_sql(CATALOG, "qa"), "GRANT")
    revoked = _privileges(layer_scoped_revokes_sql(CATALOG, "qa"), "REVOKE")

    assert granted
    assert granted.isdisjoint(revoked)


@pytest.fixture
def fake_client(tmp_path: Path) -> str:
    """Stand in for the mysql client: read stdin, print $FAKE_STDERR, exit 0.

    ``--force`` makes the real client exit 0 whatever failed, so the exit code
    here is always 0 as well.
    """
    client = tmp_path / "client"
    client.write_text(
        '#!/bin/sh\ncat >/dev/null\n[ -n "$FAKE_STDERR" ] && printf "%s\\n"'
        ' "$FAKE_STDERR" >&2\nexit 0\n'
    )
    client.chmod(0o755)
    return str(client)


@pytest.mark.parametrize(
    ("stderr", "succeeds"),
    [
        ("", True),
        (
            f"ERROR 1064 (HY000) at line 2: {NO_SUCH_GRANT_ERROR} ALL TABLES IN"
            " ALL DATABASES\nmysql: Deprecated program name.",
            True,
        ),
        ("ERROR 2003 (HY000): Can't connect to MySQL server on 'fe'", False),
        (
            f"ERROR 1064 (HY000) at line 2: {NO_SUCH_GRANT_ERROR} CATALOG c\n"
            "ERROR 5203 (42000) at line 3: Access denied",
            False,
        ),
    ],
)
def test_revoke_step_fails_only_on_unexpected_errors(
    fake_client: str, stderr: str, *, succeeds: bool
) -> None:
    result = subprocess.run(  # noqa: S603
        ["/bin/sh", "-c", revoke_step(fake_client, "STARROCKS_REVOKE_SQL")],
        env={"STARROCKS_REVOKE_SQL": "REVOKE x;", "FAKE_STDERR": stderr},
        capture_output=True,
        text=True,
        check=False,
    )

    assert (result.returncode == 0) == succeeds, result.stderr
    if not succeeds:
        assert NO_SUCH_GRANT_ERROR not in result.stderr
        assert "ERROR" in result.stderr
