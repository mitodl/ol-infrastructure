"""Guard how the OCW site picks the ocw-hugo-themes 404 template."""

import httpx
import pytest

from ol_infrastructure.applications.ocw_site.theme_404 import (
    latest_production_tag,
    themes_404_url,
)

RAW = "https://raw.githubusercontent.com/mitodl/ocw-hugo-themes"


def _client(refs: list[str], status: int = 200) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/git/matching-refs/tags/production/")
        return httpx.Response(status, json=[{"ref": f"refs/tags/{r}"} for r in refs])

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_versions_compare_as_numbers_not_text():
    """Day 10 is newer than day 9, which a string compare gets backwards."""
    tags = [
        "production/2026.11.9.1",
        "production/2026.11.10.1",
        "production/2026.2.1.1",
    ]
    assert latest_production_tag(tags) == "production/2026.11.10.1"


def test_only_production_calver_tags_count():
    """Release tags, the old v1.x tags and tombstones are not Production tags."""
    tags = [
        "v1.165.0",
        "2026.11.12.1",
        "production/not-a-version",
        "abandoned/2026.1.1.1",
    ]
    assert latest_production_tag(tags) is None


def test_the_template_is_read_from_the_newest_production_tag():
    """Production follows the newest production/ tag, so the 404 does too."""
    client = _client(["production/2026.11.5.1", "production/2026.11.6.1"])
    assert (
        themes_404_url(client) == f"{RAW}/production/2026.11.6.1/www/layouts/404.html"
    )


def test_it_falls_back_to_the_release_branch_before_the_first_production_tag():
    """Nothing is promoted yet, so the last Doof release is what Production runs."""
    assert themes_404_url(_client([])) == f"{RAW}/release/www/layouts/404.html"


def test_a_failed_lookup_stops_the_deploy_instead_of_serving_a_stale_template():
    """A GitHub error must not fall back to the frozen branch silently."""
    with pytest.raises(httpx.HTTPStatusError):
        themes_404_url(_client([], status=403))
