"""The cut jobs build a hotfix's tree before bump-version runs.

The script is run with real git against a local origin, since what it builds
is a property of the commit graph.
"""

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from bridge.settings.apps import APPS, release_job_names, release_resource_workflow
from ol_concourse.pipelines.infrastructure.k8s_apps.pipeline import (
    PREPARE_HOTFIX_SOURCE_TASK_NAME,
    build_app_pipeline,
)

PROD = "2026.9.1.1"
RELEASE_APPS = [app for app in APPS if release_resource_workflow(app)]


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(  # noqa: S603
        ["git", *args],  # noqa: S607
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _commit(repo: Path, message: str, **files: str) -> str:
    for name, content in files.items():
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_text(content)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def _cut_job(app_name: str) -> dict[str, Any]:
    pipeline = json.loads(build_app_pipeline(app_name).model_dump_json())
    name = release_job_names(app_name)["release"]
    (job,) = [job for job in pipeline["jobs"] if job["name"] == name]
    return job


def _task(job: dict[str, Any], name: str) -> dict[str, Any]:
    (step,) = [step for step in job["plan"] if step.get("task") == name]
    return step


@pytest.mark.parametrize("app_name", RELEASE_APPS)
def test_every_cut_job_prepares_the_hotfix_source_right_before_bumping(app_name):
    job = _cut_job(app_name)
    names = [step.get("task") for step in job["plan"]]
    prepare = names.index(PREPARE_HOTFIX_SOURCE_TASK_NAME)
    assert names[prepare + 1] == "bump-version"
    config = _task(job, PREPARE_HOTFIX_SOURCE_TASK_NAME)["config"]
    main = config["params"]["REPO"]
    release = config["params"]["RELEASE"]
    assert {i["name"] for i in config["inputs"]} == {main, release}
    assert [o["name"] for o in config["outputs"]] == [main], (
        "bump-version and the put must see the prepared checkout"
    )
    (create,) = [
        step
        for step in job["plan"]
        if step.get("put") == release and step["params"].get("action") == "create"
    ]
    assert create["params"]["repo_dir"] == main


@pytest.fixture(autouse=True)
def git_isolated(tmp_path, monkeypatch):
    """Keep the developer's git config (signing, hooks, identity) out of it."""
    config = tmp_path / "gitconfig"
    config.write_text(
        "[user]\n\tname = Test\n\temail = test@example.com\n"
        "[init]\n\tdefaultBranch = main\n"
    )
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")


@pytest.fixture
def world(tmp_path) -> dict[str, Any]:
    """Build an origin with PROD released and finished, then a fix among new work.

    Shaped as the release resource leaves it: the PROD tag on the pre-bump
    commit, `Release PROD` on releases/PROD merged back into main. Each
    change carries a changelog fragment, as scriv repos do.
    """
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "-q", "--bare", str(origin))
    dev = tmp_path / "dev"
    _git(tmp_path, "clone", "-q", str(origin), str(dev))
    cut = _commit(dev, "Initial", **{"app.txt": "broken\n", "changelog.d/a.md": "a"})
    _git(dev, "checkout", "-q", "-b", f"releases/{PROD}")
    _git(dev, "rm", "-q", "changelog.d/a.md")
    prod_sha = _commit(dev, f"Release {PROD}", **{"CHANGELOG.md": "a\n"})
    _git(dev, "checkout", "-q", "main")
    _git(dev, "merge", "-q", "--no-ff", f"releases/{PROD}", "-m", f"Merge {PROD}")
    _git(dev, "tag", "-a", PROD, "-m", f"Release {PROD}", cut)
    _commit(dev, "Unreleased feature", **{"changelog.d/feature.md": "feature"})
    fix = _commit(
        dev, "Fix the bug", **{"app.txt": "fixed\n", "changelog.d/fix.md": "fix"}
    )
    follow_up = _commit(dev, "Fix it properly", **{"app.txt": "fixed properly\n"})
    _commit(dev, "More unreleased work", **{"changelog.d/more.md": "more"})
    _git(dev, "push", "-q", "origin", "main", "--tags")
    return {
        "origin": origin,
        "prod_sha": prod_sha,
        "fix": fix,
        "follow_up": follow_up,
    }


def _run_task(
    tmp_path: Path, world: dict[str, Any], hotfix: str, since: str = PROD
) -> tuple[Path, subprocess.CompletedProcess[str]]:
    """Run the rendered task's script the way Concourse does, in a fresh build dir."""
    config = _task(_cut_job("learn-ai"), PREPARE_HOTFIX_SOURCE_TASK_NAME)["config"]
    build = tmp_path / "build"
    if build.exists():
        shutil.rmtree(build)
    build.mkdir()
    main = build / config["params"]["REPO"]
    _git(tmp_path, "clone", "-q", "--branch", "main", str(world["origin"]), str(main))
    release = build / config["params"]["RELEASE"]
    release.mkdir()
    (release / "hotfix").write_text(hotfix)
    (release / "since").write_text(since)
    result = subprocess.run(  # noqa: S603
        [config["run"]["path"], *config["run"]["args"]],
        cwd=build,
        env={**os.environ, **config["params"]},
        capture_output=True,
        text=True,
        check=False,
    )
    return main, result


def _files(repo: Path) -> set[str]:
    return set(_git(repo, "ls-files").split())


def test_a_normal_release_leaves_main_checked_out(tmp_path, world):
    main, result = _run_task(tmp_path, world, hotfix="")

    assert result.returncode == 0, result.stderr
    assert _git(main, "rev-parse", "HEAD") == _git(world["origin"], "rev-parse", "main")


def test_a_hotfix_bumps_production_plus_its_commits(tmp_path, world):
    """Only the hotfix's own fragment is there for the changelog to collect."""
    main, result = _run_task(
        tmp_path, world, hotfix=f"{world['fix']} {world['follow_up']}"
    )

    assert result.returncode == 0, result.stderr
    head = _git(main, "rev-parse", "HEAD")
    assert _git(main, "rev-parse", "HEAD~2") == world["prod_sha"]
    assert (main / "app.txt").read_text() == "fixed properly\n"
    assert {f for f in _files(main) if f.startswith("changelog.d/")} == {
        "changelog.d/fix.md"
    }
    log = _git(main, "log", "--format=%B", f"{world['prod_sha']}..{head}")
    assert f"(cherry picked from commit {world['fix']})" in log
    assert not _git(main, "status", "--porcelain"), "bump-version starts clean"


def test_a_hotfix_of_an_unfinished_release_starts_from_its_tag(tmp_path, world):
    """With no `Release <since>` commit on main, the tag is the base."""
    _git(world["origin"], "tag", "2026.9.5.1", world["fix"])

    main, result = _run_task(
        tmp_path, world, hotfix=world["follow_up"], since="2026.9.5.1"
    )

    assert result.returncode == 0, result.stderr
    assert _git(main, "rev-parse", "HEAD^") == world["fix"]


@pytest.mark.parametrize(
    ("hotfix", "since"),
    [
        pytest.param("FOLLOW_UP", PROD, id="does-not-apply-without-the-fix"),
        pytest.param("FIX", "", id="no-release-yet"),
        pytest.param("FIX", "2026.1.1.1", id="unknown-tag"),
        pytest.param("f" * 40, PROD, id="unknown-commit"),
    ],
)
def test_a_hotfix_it_cannot_build_is_left_to_the_release_resource(
    tmp_path, world, hotfix, since
):
    """The resource refuses with the reason; this must not fail the build first."""
    hotfix = hotfix.replace("FOLLOW_UP", world["follow_up"]).replace(
        "FIX", world["fix"]
    )

    main, result = _run_task(tmp_path, world, hotfix=hotfix, since=since)

    assert result.returncode == 0, result.stderr
    assert "bumping on main instead" in result.stderr
    assert _git(main, "rev-parse", "HEAD") == _git(world["origin"], "rev-parse", "main")
    assert not _git(main, "status", "--porcelain")
