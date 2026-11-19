"""Guard the shape of the OCW redirect dictionary that redirects.vcl reads."""

import re

import pytest

from ol_infrastructure.applications.ocw_site.vcl import redirect_dictionary

ENTRIES = sorted(redirect_dictionary().items())
VALUE = re.compile(r"^(301|302|307)\|(keep|discard)\|(?P<target>\S+)$")


@pytest.mark.parametrize(("key", "value"), ENTRIES, ids=[key for key, _ in ENTRIES])
def test_entry_is_reachable_and_well_formed(key: str, value: str):
    # redirects.vcl looks up the lowercased path with any trailing slash or
    # /index.htm removed, so any other key can never match.
    assert key == key.lower()
    assert key.startswith("/")
    assert not key.endswith("/")
    assert not key.endswith("/index.htm")

    match = VALUE.match(value)
    assert match, value
    target = match["target"]
    # A same-site target is a path; "//" would make it protocol-relative.
    assert target.startswith(("/", "https://"))
    assert not target.startswith("//")
