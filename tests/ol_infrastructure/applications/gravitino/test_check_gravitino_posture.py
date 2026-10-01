"""Tests for the rendered-config half of the Gravitino posture probe.

What matters is that the fail-open shapes the chart can produce are caught: the
chart's own defaults (``simple`` authentication, an ``anonymous`` service admin),
a security key rendered twice so that the file's last line decides, a key that
turns a control off merely by being present, and an enabled CORS filter.
"""

from ol_infrastructure.applications.gravitino.scripts.check_gravitino_posture import (
    check_rendered_config,
    parse_properties,
)

EXPECTED = {
    "gravitino.authenticators": "oauth",
    "gravitino.authorization.enable": "true",
    "gravitino.authorization.serviceAdmins": "service-account-ol-gravitino-admin",
}
FORBIDDEN = ["gravitino.authorization.impl"]
MUST_NOT_BE_TRUE = ["gravitino.iceberg-rest.enableCorsFilter"]

GOOD_CONF = """
# THE CONFIGURATION FOR authorization
gravitino.authorization.enable = true
gravitino.authorization.serviceAdmins = service-account-ol-gravitino-admin
gravitino.authenticators = oauth
gravitino.server.webserver.customFilters =
"""


def _check(conf: str) -> list[str]:
    return check_rendered_config(
        parse_properties(conf), EXPECTED, FORBIDDEN, MUST_NOT_BE_TRUE
    )


def test_expected_config_passes():
    assert _check(GOOD_CONF) == []


def test_parse_keeps_duplicates_and_empty_values():
    pairs = parse_properties("a = 1\n# a = 2\na=3\nb =\n")
    assert pairs == [("a", "1"), ("a", "3"), ("b", "")]


def test_chart_defaults_fail():
    conf = GOOD_CONF.replace("= oauth", "= simple").replace(
        "service-account-ol-gravitino-admin", "anonymous"
    )
    failures = _check(conf)
    assert any("gravitino.authenticators" in failure for failure in failures)
    assert any("serviceAdmins" in failure for failure in failures)


def test_duplicate_security_key_fails_even_when_last_value_is_right():
    conf = "gravitino.authenticators = simple\n" + GOOD_CONF
    assert _check(conf) == [
        "gravitino.authenticators occurs 2 times, expected exactly once"
    ]


def test_missing_security_key_fails():
    conf = GOOD_CONF.replace("gravitino.authenticators = oauth\n", "")
    assert _check(conf) == [
        "gravitino.authenticators occurs 0 times, expected exactly once"
    ]


def test_forbidden_key_fails_whatever_its_value():
    conf = GOOD_CONF + "gravitino.authorization.impl =\n"
    assert _check(conf) == [
        "gravitino.authorization.impl is set ('') and must be absent"
    ]


def test_enabled_cors_filter_fails():
    assert _check(GOOD_CONF + "gravitino.iceberg-rest.enableCorsFilter = true\n") == [
        "gravitino.iceberg-rest.enableCorsFilter is enabled"
    ]
    assert _check(GOOD_CONF + "gravitino.iceberg-rest.enableCorsFilter = false\n") == []
