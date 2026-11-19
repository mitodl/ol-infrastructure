"""Version a changed API client after the source release tag it was built from.

Runs in ``generate-clients`` for a source repo that releases as CalVer tags
(``YYYY.M.D.N``), in the client repo's checkout, after
``api-clients-detect-change.sh``. The pipeline watches the release's
``production/<version>`` tag, so a client is published only once its release
is in Production, and the client version names that release:

    ========================  ==============
    Tag                       Client version
    ========================  ==============
    production/2026.11.16.1   2026.1116.1
    production/2026.11.16.2   2026.1116.2
    production/2027.1.5.1     2027.105.1
    ========================  ==============

The minor is the month followed by the two-digit day, so it never has a
leading zero (valid semver) and reads only one way (``105`` is January 5,
``1105`` November 5). Every such version sorts above the ``YYYY.M.D`` ones
published before this scheme.

It writes the version to the repo-root ``VERSION``, to each package's
``package.json`` (the version ``yarn npm publish`` uses), and to
``.git/publish-tag`` (the tag the put adds to the client repo). It does
nothing when the client did not change (no ``.git/client-changed``).

``api_clients_pipeline.py`` inlines this file's text into the task
(``python3 -c``), so it must stay standard-library only and self-contained.

Environment:
    SOURCE_DIR: the source repo's checkout, which the git resource got at the
        tag. The resource names the tag in ``.git/tag``, or in
        ``.git/metadata.json`` on a resource version that lacks that file.
    TAG_REGEX: the pipeline's tag regex, which picks the tag that triggered
        the build out of the tags on its commit.
    PACKAGES: the package directories under ``src/typescript/``, separated by
        whitespace.
"""

import json
import os
import re
import sys
from pathlib import Path

_TAG = re.compile(
    r"(?:^|/)(?P<year>[0-9]{4})\.(?P<month>[0-9]{1,2})\.(?P<day>[0-9]{1,2})"
    r"\.(?P<n>[0-9]+)$"
)
_RELEASE = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+")
_PACKAGE_VERSION = re.compile(r'^(  "version": ")[^"]*(")', re.MULTILINE)
_MAX_MONTH = 12
_MAX_DAY = 31
# Sorts a legacy YYYY.M.D version above every release number of its day.
_LEGACY_DAY_END = sys.maxsize


def client_version(tag: str) -> str:
    """Return the client version for release tag *tag*, prefixed or not."""
    match = _TAG.search(tag)
    if match is None:
        msg = f"{tag!r} is not a YYYY.M.D.N release tag"
        raise ValueError(msg)
    month, day = int(match["month"]), int(match["day"])
    if not (1 <= month <= _MAX_MONTH and 1 <= day <= _MAX_DAY):
        msg = f"{tag!r} does not name a calendar date"
        raise ValueError(msg)
    return f"{int(match['year'])}.{month}{day:02d}.{int(match['n'])}"


def source_tag(source_dir: Path, tag_regex: str) -> str:
    """Return the tag matching *tag_regex* that *source_dir* is checked out at.

    The git resource writes ``git tag --points-at HEAD``, one tag per line,
    and a Production tag shares its commit with the release tag, so the file
    holds both (``2026.11.17.1`` and ``production/2026.11.17.1``). Exactly one
    line may match the pipeline's tag regex.
    """
    text = ""
    tag_file = source_dir / ".git" / "tag"
    if tag_file.is_file():
        text = tag_file.read_text()
    metadata = source_dir / ".git" / "metadata.json"
    if not text.strip() and metadata.is_file():
        for entry in json.loads(metadata.read_text()):
            if entry.get("name") == "tag" and entry.get("value"):
                text = str(entry["value"])
    tags = [line.strip() for line in text.splitlines() if line.strip()]
    if not tags:
        msg = f"{source_dir} has no .git/tag or tag metadata; is it a tag checkout?"
        raise ValueError(msg)
    matching = [tag for tag in tags if re.search(tag_regex, tag)]
    if len(matching) != 1:
        msg = f"expected one of the tags {tags} to match {tag_regex!r}"
        raise ValueError(msg)
    return matching[0]


def _release_key(version: str) -> tuple[int, int, int] | None:
    """Return a ``X.Y.Z`` version's numbers, ignoring any prerelease suffix.

    A legacy bumpver ``YYYY.M.D[-N]`` version (minor 1-12) is read as sorting
    above every release-scheme version of that day, ``YYYY.MDD.<any>``. The
    two cannot be confused, because a release-scheme minor is at least 101.
    Without this, every release-scheme version sorts above a legacy one, so
    the first client after the cutover could publish an older release's API
    (a Production rollback re-run before any ``production/*`` tag exists)
    above the newer one bumpver published at cut time.

    The legacy version names only the day, not which of that day's releases
    it was generated from (mitxonline's ``2026.11.18-1`` came from release
    ``2026.11.18.2``; the ``-1`` counts regenerations, not releases), so a
    release from the same day may be older than it. Refusing the whole day
    is the one floor that cannot let an older API through. It costs one day
    at most, once: a client change from a release on the legacy version's
    day waits for the next day's release.
    """
    match = _RELEASE.match(version)
    if match is None:
        return None
    major, minor, patch = (int(part) for part in match[0].split("."))
    if 1 <= minor <= _MAX_MONTH and 1 <= patch <= _MAX_DAY:
        return major, minor * 100 + patch, _LEGACY_DAY_END
    return major, minor, patch


def set_version(repo: Path, version: str, packages: list[str]) -> None:
    """Write *version* everywhere the client repo records it.

    Raises ValueError when *version* does not sort above the current
    ``VERSION``: npm refuses a version it already has, and an older one would
    not reach anyone who takes the newest client.
    """
    version_file = repo / "VERSION"
    current = version_file.read_text().strip() if version_file.is_file() else ""
    current_key = _release_key(current)
    new_key = _release_key(version)
    if current_key is not None and new_key is not None and new_key <= current_key:
        msg = (
            f"the client changed, but this release's version {version} does not "
            f"sort above the current {current}. Was an older release re-run, or "
            "did the generator or its configs change since this release was "
            "published? The change ships with the next release."
        )
        raise ValueError(msg)
    if not packages:
        msg = "PACKAGES names no package"
        raise ValueError(msg)
    for package in packages:
        manifest = repo / "src" / "typescript" / package / "package.json"
        text, count = _PACKAGE_VERSION.subn(
            rf"\g<1>{version}\g<2>", manifest.read_text(), count=1
        )
        if count != 1:
            msg = f'{manifest} has no top-level "version" line'
            raise ValueError(msg)
        manifest.write_text(text)
    version_file.write_text(f"{version}\n")
    (repo / ".git" / "publish-tag").write_text(f"{version}\n")


def main() -> None:
    """Version the client in the current directory, if it changed."""
    repo = Path()
    if not (repo / ".git" / "client-changed").exists():
        print("The client is unchanged: not setting a version.")  # noqa: T201
        return
    try:
        tag = source_tag(Path(os.environ["SOURCE_DIR"]), os.environ["TAG_REGEX"])
        version = client_version(tag)
        set_version(repo, version, os.environ.get("PACKAGES", "").split())
    except ValueError as error:
        sys.exit(f"Not versioning the client: {error}.")
    print(f"Client version {version}, from release tag {tag}.")  # noqa: T201


if __name__ == "__main__":
    main()
