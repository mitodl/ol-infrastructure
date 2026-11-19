"""Guard what triggers an API client's npm publish.

npm refuses to publish a version it already has (HTTP 403). The client repo's
VERSION only moves when ``generate-clients`` bumps it, so a publish job that
also triggers on the Node image republishes that same version every time the
image digest moves, and fails. On 2026-11-05 a ``node:24-slim`` digest bump
re-triggered the mitxonline and mit-learn publish jobs on an unchanged client
repo, and both failed that way.

A variant with ``prerelease_branch`` also publishes ``publish-next-*``
prereleases. Those mint a fresh version per build, so the same rule applies for
a different reason: a new image digest is not a new client.

A client is published only when it changed, and a variant on release tags
watches ``production/<version>`` tags, so it publishes only promoted releases,
versioned after the release (hq discussion 14319, comment 19690418).
"""

import json
import re
import subprocess
from pathlib import Path
from typing import Any

import pytest

from bridge.settings.apps import PRODUCTION_TAG_PREFIX
from ol_concourse.pipelines.libraries.api_clients_pipeline import (
    generate_api_client_pipeline,
)
from ol_concourse.pipelines.libraries.configuration import (
    PIPELINE_CONFIGS,
    PRODUCTION_RELEASE_TAG_REGEX,
)
from ol_concourse.pipelines.libraries.scripts import api_clients_version_from_tag
from ol_concourse.pipelines.libraries.scripts.api_clients_version_from_tag import (
    client_version,
    set_version,
    source_tag,
)

GENERATE_JOB = "generate-clients"
SCRIPTS = Path(__file__).parents[2] / "src/ol_concourse/pipelines/libraries/scripts"
PRERELEASE_VARIANTS = [
    variant
    for variant, config in PIPELINE_CONFIGS.items()
    if config.get("prerelease_branch")
]
TAG_VARIANTS = [
    variant
    for variant, config in PIPELINE_CONFIGS.items()
    if config.get("source_repo_tag_regex")
]
BRANCH_VARIANTS = [v for v in PIPELINE_CONFIGS if v not in TAG_VARIANTS]


def _pipeline(variant: str) -> dict[str, Any]:
    pipeline = generate_api_client_pipeline(**PIPELINE_CONFIGS[variant])
    return json.loads(pipeline.model_dump_json(exclude_none=True))


def _publish_jobs(variant: str) -> list[dict[str, Any]]:
    """Return the stable publish jobs, which follow generate-clients."""
    jobs = [
        job
        for job in _pipeline(variant)["jobs"]
        if job["name"].startswith("publish-")
        and not job["name"].startswith("publish-next-")
    ]
    assert jobs, f"{variant} has no publish job"
    return jobs


def _prerelease_jobs(variant: str) -> list[dict[str, Any]]:
    return [
        job
        for job in _pipeline(variant)["jobs"]
        if job["name"].startswith("publish-next-")
    ]


def _prerelease_source(variant: str) -> str:
    config = PIPELINE_CONFIGS[variant]
    return f"{config['source_repo_name']}-{config['prerelease_branch']}"


def _get(job: dict[str, Any], resource: str) -> dict[str, Any]:
    (step,) = [s for s in job["plan"] if s.get("get") == resource]
    return step


def _generate_job(variant: str) -> dict[str, Any]:
    (job,) = [j for j in _pipeline(variant)["jobs"] if j["name"] == GENERATE_JOB]
    return job


def _step_names(job: dict[str, Any]) -> list[str]:
    return [
        s.get("task") or s.get("put") or s.get("get") or s.get("load_var") or ""
        for s in job["plan"]
    ]


def _subpaths(variant: str) -> list[str]:
    subpaths = PIPELINE_CONFIGS[variant]["client_repo_subpath"]
    return [subpaths] if isinstance(subpaths, str) else subpaths


@pytest.mark.parametrize("variant", PIPELINE_CONFIGS)
def test_a_new_node_image_does_not_trigger_a_publish(variant):
    for job in _publish_jobs(variant):
        assert not _get(job, "node-image").get("trigger"), (
            f"{variant}/{job['name']} republishes the same version on every "
            "Node image digest bump, and npm refuses that"
        )


@pytest.mark.parametrize("variant", PIPELINE_CONFIGS)
def test_a_publish_still_triggers_on_a_generated_client(variant):
    """The one trigger a publish needs: generate-clients pushed a new version."""
    client_repo = PIPELINE_CONFIGS[variant]["client_repo_name"]
    for job in _publish_jobs(variant):
        step = _get(job, client_repo)
        assert step["trigger"] is True
        assert step["passed"] == [GENERATE_JOB]


def test_every_variant_publishes_prereleases_off_main():
    """The stable client waits for promotion (Production for a tag variant,
    the `release` branch for mit-learn), so `next` is the only client for an
    API that has merged or is in RC.
    """
    assert list(PIPELINE_CONFIGS) == PRERELEASE_VARIANTS
    for config in PIPELINE_CONFIGS.values():
        assert config["prerelease_branch"] == "main"


@pytest.mark.parametrize("variant", PIPELINE_CONFIGS)
def test_no_prerelease_jobs_without_a_prerelease_branch(variant):
    config = {**PIPELINE_CONFIGS[variant], "prerelease_branch": None}
    pipeline = json.loads(
        generate_api_client_pipeline(**config).model_dump_json(exclude_none=True)
    )
    assert not [j for j in pipeline["jobs"] if j["name"].startswith("publish-next-")]
    resources = [r["name"] for r in pipeline["resources"]]
    assert _prerelease_source(variant) not in resources


@pytest.mark.parametrize("variant", PRERELEASE_VARIANTS)
def test_prerelease_source_watches_spec_changes_on_its_branch(variant):
    config = PIPELINE_CONFIGS[variant]
    (resource,) = [
        r
        for r in _pipeline(variant)["resources"]
        if r["name"] == _prerelease_source(variant)
    ]
    assert resource["source"]["uri"] == config["source_repo_uri"]
    assert resource["source"]["branch"] == config["prerelease_branch"]
    assert resource["source"]["paths"] == ["openapi/specs/*.yaml"]


@pytest.mark.parametrize("variant", PRERELEASE_VARIANTS)
def test_a_prerelease_triggers_only_on_a_spec_change(variant):
    config = PIPELINE_CONFIGS[variant]
    subpaths = config["client_repo_subpath"]
    jobs = _prerelease_jobs(variant)
    assert [job["name"] for job in jobs] == [
        f"publish-next-{subpath}"
        for subpath in ([subpaths] if isinstance(subpaths, str) else subpaths)
    ]
    for job in jobs:
        triggers = [s["get"] for s in job["plan"] if s.get("trigger")]
        assert triggers == [_prerelease_source(variant)]
        assert "passed" not in _get(job, config["client_repo_name"])
        # Newest spec wins `next` only if builds publish in trigger order.
        assert job["serial"] is True


@pytest.mark.parametrize("variant", PRERELEASE_VARIANTS)
def test_a_prerelease_generates_from_the_branch_under_the_source_name(variant):
    """The generator configs read ``<source_repo_name>/openapi/specs/...``."""
    config = PIPELINE_CONFIGS[variant]
    for job in _prerelease_jobs(variant):
        (generate,) = [s for s in job["plan"] if s.get("task") == "generate-apis"]
        assert {
            "name": _prerelease_source(variant),
            "path": config["source_repo_name"],
        } in generate["config"]["inputs"]


@pytest.mark.parametrize("variant", PRERELEASE_VARIANTS)
def test_a_prerelease_commits_nothing_and_publishes_under_next(variant):
    for job in _prerelease_jobs(variant):
        assert not [s for s in job["plan"] if "put" in s]
        (publish,) = [s for s in job["plan"] if s.get("task") == job["name"]]
        script = publish["config"]["run"]["args"][-1]
        assert "yarn npm publish --access public --tag next" in script
        # A prerelease version, never a plain one that could pass for stable.
        assert 'npm pkg set "version=$version"' in script
        # `yarn version` spawns git, which node:*-slim lacks.
        assert "yarn version" not in script
        assert '$(date -u +%Y.%-m%d).0-dev.$(date -u +%s).g${SOURCE_SHA:0:7}"' in script
        assert publish["config"]["params"]["SOURCE_SHA"] == (
            f"((.:{_prerelease_source(variant)}-git-rev))"
        )


# --- Publish on promotion, versioned after the release -----------------------


def test_the_tag_variants_are_the_release_resource_apps():
    assert TAG_VARIANTS == ["ol-analytics-api", "mitxonline"]
    assert BRANCH_VARIANTS == ["mit-learn"]


def test_the_production_tag_regex_matches_only_promoted_releases():
    assert PRODUCTION_RELEASE_TAG_REGEX.startswith(f"^{PRODUCTION_TAG_PREFIX}")
    # The resource matches with `grep -E`; this pattern means the same in ERE
    # and in Python's re.
    matches = re.compile(PRODUCTION_RELEASE_TAG_REGEX).search
    assert matches("production/2026.11.16.1")
    assert matches("production/2027.1.5.12")
    for tag in [
        "2026.11.16.1",  # cut, not promoted
        "abandoned/2026.11.16.1",
        "v0.204.0",
        "production/2026.11.16",
        "xproduction/2026.11.16.1",
        "production/2026.11.16.1-rc",
    ]:
        assert not matches(tag), tag


@pytest.mark.parametrize("variant", TAG_VARIANTS)
def test_a_tag_variant_watches_production_tags(variant):
    (resource,) = [
        r
        for r in _pipeline(variant)["resources"]
        if r["name"] == PIPELINE_CONFIGS[variant]["source_repo_name"]
    ]
    assert resource["source"]["version_type"] == "tags"
    assert resource["source"]["tag_regex"] == PRODUCTION_RELEASE_TAG_REGEX


@pytest.mark.parametrize("variant", PIPELINE_CONFIGS)
def test_generate_clients_versions_and_commits_only_after_checking_for_a_change(
    variant,
):
    config = PIPELINE_CONFIGS[variant]
    names = _step_names(_generate_job(variant))
    version_step = "version-from-tag" if variant in TAG_VARIANTS else "bump-version"
    order = [
        names.index("generate-apis"),
        names.index("detect-client-change"),
        names.index(version_step),
        names.index("commit-changes"),
        names.index(config["client_repo_name"], names.index("commit-changes")),
    ]
    assert order == sorted(order)
    other = "bump-version" if variant in TAG_VARIANTS else "version-from-tag"
    assert other not in names


@pytest.mark.parametrize("variant", PIPELINE_CONFIGS)
def test_the_put_tags_only_a_new_version(variant):
    """The tag comes from a file the version step fills only on a change."""
    repo = PIPELINE_CONFIGS[variant]["client_repo_name"]
    (put,) = [s for s in _generate_job(variant)["plan"] if s.get("put") == repo]
    assert put["params"] == {"repository": repo, "tag": f"{repo}/.git/publish-tag"}


@pytest.mark.parametrize("variant", TAG_VARIANTS)
def test_a_tag_variant_versions_from_the_tag(variant):
    config = PIPELINE_CONFIGS[variant]
    (step,) = [
        s for s in _generate_job(variant)["plan"] if s.get("task") == "version-from-tag"
    ]
    task = step["config"]
    assert {"name": config["source_repo_name"]} in task["inputs"]
    assert task["params"] == {
        "SOURCE_DIR": f"../{config['source_repo_name']}",
        "TAG_REGEX": PRODUCTION_RELEASE_TAG_REGEX,
        "PACKAGES": " ".join(_subpaths(variant)),
    }
    assert task["run"]["path"] == "python3"
    assert task["run"]["dir"] == config["client_repo_name"]
    assert task["run"]["args"] == [
        "-c",
        (SCRIPTS / "api_clients_version_from_tag.py").read_text(),
    ]


@pytest.mark.parametrize("variant", PIPELINE_CONFIGS)
def test_a_publish_skips_a_version_npm_already_has(variant):
    for job in _publish_jobs(variant):
        (task,) = [s for s in job["plan"] if "task" in s]
        script = task["config"]["run"]["args"][-1]
        check = script.index('npm view "$name@$version" version')
        assert check < script.index("yarn npm publish")


@pytest.mark.parametrize("variant", PRERELEASE_VARIANTS)
def test_a_prerelease_publishes_only_a_changed_client(variant):
    for job in _prerelease_jobs(variant):
        (publish,) = [s for s in job["plan"] if s.get("task") == job["name"]]
        script = publish["config"]["run"]["args"][-1]
        assert "for channel in next latest; do" in script
        compare = script.index('diff -r -q dist "$published/package/dist"')
        assert script.index("yarn build") < compare
        assert compare < script.index("yarn npm publish")
        # A client that matches `latest` moves `next` there, so `@next` does
        # not keep serving an API that main reverted.
        retag = script.index('yarn npm tag add "$name@$latest_version" next')
        assert compare < retag < script.index("yarn npm publish")


@pytest.mark.parametrize(
    ("tag", "version"),
    [
        ("production/2026.11.16.1", "2026.1116.1"),
        ("production/2026.11.16.2", "2026.1116.2"),
        ("production/2027.1.5.1", "2027.105.1"),
        ("production/2026.11.5.1", "2026.1105.1"),
        ("production/2026.12.31.10", "2026.1231.10"),
        ("2026.11.16.1", "2026.1116.1"),
    ],
)
def test_the_client_version_names_the_release(tag, version):
    assert client_version(tag) == version


@pytest.mark.parametrize(
    "tag", ["production/2026.11.16", "production/2026.13.1.1", "production/2026.1.0.1"]
)
def test_a_tag_that_is_not_a_release_has_no_client_version(tag):
    with pytest.raises(ValueError, match=r"release tag|calendar date"):
        client_version(tag)


def test_every_new_version_sorts_above_the_old_scheme():
    """Each part is numeric, so this is semver's (and PEP 440's) order."""

    def key(version: str) -> tuple[int, ...]:
        return tuple(int(part) for part in version.split("."))

    old = ["2026.11.16", "2026.11.5", "2026.12.31"]
    new = [client_version("production/2026.11.17.1"), client_version("2027.1.5.1")]
    assert max(map(key, old)) < min(map(key, new))


def test_the_tag_comes_from_the_resource_tag_file(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "tag").write_text("production/2026.11.16.1\n")
    assert (
        source_tag(tmp_path, PRODUCTION_RELEASE_TAG_REGEX) == "production/2026.11.16.1"
    )


def test_the_production_tag_is_picked_out_of_the_tags_on_its_commit(tmp_path):
    """The resource writes `git tag --points-at HEAD`, and a Production tag
    shares its commit with the release tag (and a depth-1 clone fetches both).
    """
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "tag").write_text("2026.11.16.1\nproduction/2026.11.16.1\n")
    assert (
        source_tag(tmp_path, PRODUCTION_RELEASE_TAG_REGEX) == "production/2026.11.16.1"
    )


def test_the_tag_falls_back_to_the_resource_metadata(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "tag").write_text("\n")
    (tmp_path / ".git" / "metadata.json").write_text(
        json.dumps(
            [
                {"name": "commit", "value": "a" * 40},
                {"name": "tag", "value": "2026.11.16.2\nproduction/2026.11.16.2"},
            ]
        )
    )
    assert (
        source_tag(tmp_path, PRODUCTION_RELEASE_TAG_REGEX) == "production/2026.11.16.2"
    )


@pytest.mark.parametrize(
    ("tags", "match"),
    [
        ("", "tag checkout"),
        ("2026.11.16.1\n", "expected one"),
        ("production/2026.11.16.1\nproduction/2026.11.16.2\n", "expected one"),
    ],
)
def test_a_checkout_without_exactly_one_matching_tag_is_refused(tmp_path, tags, match):
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "tag").write_text(tags)
    with pytest.raises(ValueError, match=match):
        source_tag(tmp_path, PRODUCTION_RELEASE_TAG_REGEX)


PACKAGE_JSON = """{
  "name": "@mitodl/%s",
  "version": "2026.11.16",
  "dependencies": {
    "axios": "^1.0.0"
  }
}
"""


def _client_repo(tmp_path: Path, packages: list[str], version: str) -> Path:
    repo = tmp_path / "clients"
    (repo / ".git").mkdir(parents=True)
    (repo / "VERSION").write_text(f"{version}\n")
    for package in packages:
        directory = repo / "src" / "typescript" / package
        directory.mkdir(parents=True)
        (directory / "package.json").write_text(PACKAGE_JSON % package)
    return repo


def test_set_version_writes_every_place_the_version_lives(tmp_path):
    packages = ["one-api-axios", "two-api-axios"]
    repo = _client_repo(tmp_path, packages, "2026.11.16")

    set_version(repo, "2026.1117.1", packages)

    assert (repo / "VERSION").read_text() == "2026.1117.1\n"
    assert (repo / ".git" / "publish-tag").read_text() == "2026.1117.1\n"
    for package in packages:
        manifest = repo / "src" / "typescript" / package / "package.json"
        assert manifest.read_text() == (PACKAGE_JSON % package).replace(
            '"version": "2026.11.16"', '"version": "2026.1117.1"'
        )


@pytest.mark.parametrize("current", ["2026.1117.1", "2026.1117.2", "2026.1201.1"])
def test_set_version_refuses_a_version_that_does_not_sort_higher(tmp_path, current):
    repo = _client_repo(tmp_path, ["one-api-axios"], current)

    with pytest.raises(ValueError, match="does not sort above"):
        set_version(repo, "2026.1117.1", ["one-api-axios"])
    assert (repo / "VERSION").read_text() == f"{current}\n"
    assert not (repo / ".git" / "publish-tag").exists()


@pytest.mark.parametrize(
    ("current", "version"),
    [
        ("2026.11.18", "2026.1110.1"),
        ("2026.11.18-1", "2026.1110.1"),
        ("2026.12.1", "2026.1110.1"),
        # The legacy version names only its day. mitxonline's 2026.11.18-1 came
        # from release 2026.11.18.2, so 2026.11.18.1 (or any same-day release)
        # may be an older API than the one it holds.
        ("2026.11.18-1", "2026.1118.1"),
        ("2026.11.18", "2026.1118.1"),
        ("2026.11.18", "2026.1118.99"),
    ],
)
def test_a_legacy_version_is_a_floor_for_its_day_and_older(tmp_path, current, version):
    """Bumpver's YYYY.M.D[-N] sorts above every release of its day, so the
    first tag-derived client cannot publish an older release's API above it (a
    Production rollback re-run before any production/* tag exists).
    """
    repo = _client_repo(tmp_path, ["one-api-axios"], current)

    with pytest.raises(ValueError, match="does not sort above"):
        set_version(repo, version, ["one-api-axios"])
    assert (repo / "VERSION").read_text() == f"{current}\n"


@pytest.mark.parametrize("current", ["2026.11.12", "2026.11.18", "2026.11.18-1"])
def test_a_release_from_after_the_legacy_version_s_day_is_accepted(tmp_path, current):
    repo = _client_repo(tmp_path, ["one-api-axios"], current)

    set_version(repo, "2026.1119.1", ["one-api-axios"])

    assert (repo / "VERSION").read_text() == "2026.1119.1\n"


def test_the_version_step_does_nothing_for_an_unchanged_client(tmp_path, monkeypatch):
    repo = _client_repo(tmp_path, ["one-api-axios"], "2026.11.16")
    monkeypatch.chdir(repo)
    monkeypatch.setenv("SOURCE_DIR", str(tmp_path / "missing"))
    monkeypatch.setenv("PACKAGES", "one-api-axios")

    api_clients_version_from_tag.main()

    assert (repo / "VERSION").read_text() == "2026.11.16\n"


def test_the_version_step_versions_a_changed_client(tmp_path, monkeypatch):
    repo = _client_repo(tmp_path, ["one-api-axios"], "2026.11.16")
    (repo / ".git" / "client-changed").touch()
    source = tmp_path / "mitxonline"
    (source / ".git").mkdir(parents=True)
    (source / ".git" / "tag").write_text("2026.11.17.1\nproduction/2026.11.17.1\n")
    monkeypatch.chdir(repo)
    monkeypatch.setenv("SOURCE_DIR", str(source))
    monkeypatch.setenv("TAG_REGEX", PRODUCTION_RELEASE_TAG_REGEX)
    monkeypatch.setenv("PACKAGES", "one-api-axios")

    api_clients_version_from_tag.main()

    assert (repo / "VERSION").read_text() == "2026.1117.1\n"


# --- The shell steps, run against a real git checkout -------------------------


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(  # noqa: S603
        ["git", "-C", str(repo), *args],  # noqa: S607
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def _run(script: str, repo: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603
        ["sh", "-c", (SCRIPTS / script).read_text()],  # noqa: S607
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
        env={
            "PATH": "/usr/bin:/bin",
            "HOME": str(repo.parent),
            "SOURCE_REPO_NAME": "mitxonline",
            "OPEN_REV": "a" * 40,
        },
    )


@pytest.fixture
def generated_repo(tmp_path):
    repo = tmp_path / "clients"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "api.ts").write_text("export const a = 1;\n")
    (repo / "VERSION").write_text("2026.11.16\n")
    _git(tmp_path, "init", "-q", str(repo))
    _git(repo, "add", ".")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init")
    return repo


def test_an_unchanged_client_is_neither_versioned_nor_committed(generated_repo):
    head = _git(generated_repo, "rev-parse", "HEAD")
    # An untracked file is not something the commit step would stage.
    (generated_repo / "src" / "new.ts").write_text("export const b = 2;\n")

    _run("api-clients-detect-change.sh", generated_repo)
    bump = _run("api-clients-bumpver.sh", generated_repo)
    _run("api-clients-commit-changes.sh", generated_repo)

    assert not (generated_repo / ".git" / "client-changed").exists()
    assert (generated_repo / ".git" / "publish-tag").read_text() == ""
    assert "not bumping" in bump.stdout
    assert _git(generated_repo, "rev-parse", "HEAD") == head


def test_a_changed_client_is_committed(generated_repo):
    head = _git(generated_repo, "rev-parse", "HEAD")
    (generated_repo / "src" / "api.ts").write_text("export const a = 2;\n")

    _run("api-clients-detect-change.sh", generated_repo)
    # What the version step does once it sees the marker.
    (generated_repo / "VERSION").write_text("2026.1117.1\n")
    (generated_repo / ".git" / "publish-tag").write_text("2026.1117.1\n")
    _run("api-clients-commit-changes.sh", generated_repo)

    assert (generated_repo / ".git" / "client-changed").exists()
    assert _git(generated_repo, "rev-parse", "HEAD^") == head
    assert _git(generated_repo, "show", "--stat", "--format=%s", "HEAD").startswith(
        f"Generated clients for mitxonline from rev: {'a' * 40}\n"
    )
    assert _git(generated_repo, "status", "--porcelain", "--untracked-files=no") == ""


def test_a_rerun_after_a_change_starts_from_a_clean_marker(generated_repo):
    (generated_repo / ".git" / "client-changed").touch()
    (generated_repo / ".git" / "publish-tag").write_text("2026.1117.1\n")

    _run("api-clients-detect-change.sh", generated_repo)

    assert not (generated_repo / ".git" / "client-changed").exists()
    assert (generated_repo / ".git" / "publish-tag").read_text() == ""
