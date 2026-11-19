"""Find the ocw-hugo-themes 404 template that Fastly serves for the OCW site.

ocw-hugo-themes is released as tags only, and Production follows the newest
``production/<version>`` tag, which the release workflow creates after a
release is approved. The template is read from that tag so the 404 page tracks
what Production runs. Until the first such tag exists the ``release`` branch,
which the retired Doof flow last advanced, is the only record of what
Production runs, so it is the fallback.
"""

import re

import httpx

from bridge.settings.apps import PRODUCTION_TAG_PREFIX

THEMES_REPO = "mitodl/ocw-hugo-themes"
TEMPLATE_PATH = "www/layouts/404.html"
LEGACY_BRANCH = "release"
_VERSION = re.compile(r"[0-9]{4}\.[0-9]+\.[0-9]+\.[0-9]+")


def latest_production_tag(tag_names: list[str]) -> str | None:
    """Return the newest ``production/<calver>`` tag among *tag_names*.

    Versions compare as numbers, so ``2026.11.10.1`` is newer than
    ``2026.11.9.1``. Names that are not a Production calver tag are ignored.
    """
    versions = [
        name.removeprefix(PRODUCTION_TAG_PREFIX)
        for name in tag_names
        if name.startswith(PRODUCTION_TAG_PREFIX)
        and _VERSION.fullmatch(name.removeprefix(PRODUCTION_TAG_PREFIX))
    ]
    if not versions:
        return None
    newest = max(versions, key=lambda v: tuple(int(part) for part in v.split(".")))
    return f"{PRODUCTION_TAG_PREFIX}{newest}"


def themes_404_url(client: httpx.Client | None = None) -> str:
    """Return the raw URL of the 404 template Production should serve.

    Raises when GitHub cannot be asked, so a failed lookup stops the deploy
    rather than quietly serving the template of an older release.
    """
    http = client or httpx
    response = http.get(
        f"https://api.github.com/repos/{THEMES_REPO}/git/matching-refs/tags/"
        f"{PRODUCTION_TAG_PREFIX}",
        headers={"Accept": "application/vnd.github+json"},
        timeout=30,
    )
    response.raise_for_status()
    tag = latest_production_tag(
        [ref["ref"].removeprefix("refs/tags/") for ref in response.json()]
    )
    ref = tag or LEGACY_BRANCH
    return f"https://raw.githubusercontent.com/{THEMES_REPO}/{ref}/{TEMPLATE_PATH}"
