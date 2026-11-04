"""Guard the tag-only release pipeline and the script that promotes a release.

A tag-only app (``release_kind="tag"``) deploys nothing itself; its consumers
check the repository out at a tag. These tests pin the four jobs the release
bot drives, that nothing in them deploys, that the Production job checks the
gate before it writes the ``production/<version>`` tag, and what
``promote_tag.py`` does against a fake GitHub.
"""

import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, ClassVar

import pytest

from bridge.settings.apps import (
    APPS,
    PRODUCTION_TAG_PREFIX,
    release_job_names,
    release_kind,
    release_resource_workflow,
)
from ol_concourse.pipelines.infrastructure.k8s_apps.pipeline import (
    _PROMOTE_TAG_SCRIPT,
    ASSERT_GATE_TASK_NAME,
    CHECK_PROMOTE_TAG_TASK_NAME,
    CLASSIFY_TASK_NAME,
    PROMOTE_TAG_TASK_NAME,
    SUPERSEDE_TASK_NAME,
    build_app_pipeline,
)

APP = "ocw-hugo-themes"
TAG_APPS = [app for app in APPS if release_kind(app) == "tag"]
VERSION = "2026.11.2.1"
COMMIT = "a" * 40
OTHER = "b" * 40
TAG_OBJECT = "c" * 40


def _pipeline(app_name: str = APP) -> dict[str, Any]:
    return json.loads(build_app_pipeline(app_name).model_dump_json())


def _job(name: str, app_name: str = APP) -> dict[str, Any]:
    (job,) = [j for j in _pipeline(app_name)["jobs"] if j["name"] == name]
    return job


def _index(plan: list[dict[str, Any]], key: str, name: str, nth: int = 0) -> int:
    matches = [i for i, step in enumerate(plan) if step.get(key) == name]
    if len(matches) <= nth:
        steps = [next(iter(step.items())) for step in plan]
        msg = f"no {key!r} step #{nth} named {name!r}; steps are {steps}"
        raise AssertionError(msg)
    return matches[nth]


def test_both_ocw_hugo_repos_are_tag_only_and_flipped():
    """ocw-studio reads their tags, so the release workflow owns them."""
    assert set(TAG_APPS) == {"ocw-hugo-themes", "ocw-hugo-projects"}
    assert all(release_resource_workflow(app) for app in TAG_APPS)


@pytest.mark.parametrize("app_name", TAG_APPS)
def test_the_jobs_are_the_ones_the_release_bot_drives(app_name):
    names = {job["name"] for job in _pipeline(app_name)["jobs"]}
    jobs = release_job_names(app_name)
    assert names == {
        jobs["release"],
        f"deploy-{app_name}-rc",
        jobs["production"],
        jobs["abandon"],
    }
    assert jobs["release"] == f"cut-{app_name}-release"
    assert jobs["production"] == f"promote-{app_name}-production"


def test_k8s_apps_keep_their_job_names():
    assert release_job_names("xpro") == {
        "release": "build-xpro-release-image",
        "production": "deploy-ol-application-xpro-production",
        "abandon": "abandon-xpro-release",
    }


def test_nothing_in_the_pipeline_builds_or_deploys():
    pipeline = _pipeline()
    types = {resource["type"] for resource in pipeline["resources"]}
    assert types == {"git", "release", "github-issues", "github-deployments"}
    assert {rtype["name"] for rtype in pipeline["resource_types"]} == {
        "release",
        "github-issues",
        "github-deployments",
    }


def test_the_cut_job_is_not_triggered_and_cuts_the_bumped_release():
    plan = _job(f"cut-{APP}-release")["plan"]
    assert all(not step.get("trigger") for step in plan if "get" in step)
    (create,) = [step for step in plan if step.get("put") == f"{APP}-release"]
    assert create["params"]["action"] == "create"
    assert create["params"]["commit_hash"] == "((.:hotfix))"
    assert _index(plan, "task", "bump-version") < plan.index(create)


def test_rc_posts_the_checklist_issue_without_a_preview():
    plan = _job(f"deploy-{APP}-rc")["plan"]
    release = plan[_index(plan, "get", f"{APP}-release")]
    assert release["trigger"] is True
    assert release["passed"] == [f"cut-{APP}-release"]
    classify = plan[_index(plan, "task", CLASSIFY_TASK_NAME)]
    assert "PREVIEW_SUMMARY_FILE" not in classify["config"]["params"]
    assert "INFRA_REF_FILE" not in classify["config"]["params"]
    issue = plan[_index(plan, "put", f"{APP}-release-issue")]
    assert issue["params"]["skip_if_file"] == "promotion/skip"
    assert issue["params"]["skip_if_tag_missing"] == "((.:release_tag))"
    assert plan[-1]["task"] == SUPERSEDE_TASK_NAME


def test_production_checks_the_gate_before_it_promotes():
    plan = _job(f"promote-{APP}-production")["plan"]
    gate = _index(plan, "get", f"{APP}-release-gate")
    assert plan[gate]["trigger"] is True
    assert plan[gate]["version"] == "every"
    check = _index(plan, "task", ASSERT_GATE_TASK_NAME)
    assert "INFRA_REF_FILE" not in plan[check]["config"]["params"]
    check_tag = _index(plan, "task", CHECK_PROMOTE_TAG_TASK_NAME)
    start = _index(plan, "put", f"{APP}-deployment-production")
    promote = _index(plan, "task", PROMOTE_TAG_TASK_NAME)
    finish = _index(plan, "put", f"{APP}-deployment-production", nth=1)
    assert gate < check < check_tag < start < promote < finish
    params = plan[promote]["config"]["params"]
    assert params["TAG_PREFIX"] == PRODUCTION_TAG_PREFIX
    assert params["GITHUB_REPOSITORY"] == f"mitodl/{APP}"
    assert "CHECK_ONLY" not in params
    assert plan[check_tag]["config"]["params"] == {**params, "CHECK_ONLY": "true"}
    assert plan[-1]["put"] == f"{APP}-release"
    assert plan[-1]["params"]["action"] == "finish"


def test_production_takes_the_release_rc_ran():
    plan = _job(f"promote-{APP}-production")["plan"]
    release = plan[_index(plan, "get", f"{APP}-release")]
    assert release["passed"] == [f"deploy-{APP}-rc"]
    assert not release.get("trigger")


def test_every_job_shares_one_serial_group():
    """A cut or abandon cannot delete the release tag mid-promotion."""
    assert {tuple(job["serial_groups"]) for job in _pipeline()["jobs"]} == {
        (f"{APP}-release",)
    }


def test_k8s_abandon_jobs_stay_out_of_a_serial_group():
    (abandon,) = [
        job
        for job in json.loads(build_app_pipeline("xpro").model_dump_json())["jobs"]
        if job["name"] == "abandon-xpro-release"
    ]
    assert not abandon.get("serial_groups")


class _FakeGitHub(BaseHTTPRequestHandler):
    """The git refs API for one repository, plus an installation token."""

    refs: ClassVar[dict[str, dict[str, str]]] = {}
    tags: ClassVar[dict[str, dict[str, str]]] = {}
    race: ClassVar[bool] = False
    created: ClassVar[list[dict[str, str]]] = []

    def log_message(self, *_args: Any) -> None:
        pass

    def _reply(self, status: int, body: Any = None) -> None:
        payload = json.dumps({} if body is None else body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:
        prefix = f"/repos/mitodl/{APP}/git/"
        assert self.path.startswith(prefix), self.path
        rest = self.path.removeprefix(prefix)
        if rest.startswith("ref/tags/"):
            ref = self.refs.get(rest.removeprefix("ref/tags/"))
            self._reply(200, {"object": ref}) if ref else self._reply(404)
        elif rest.startswith("matching-refs/tags/"):
            match = rest.removeprefix("matching-refs/tags/")
            self._reply(
                200,
                [
                    {"ref": f"refs/tags/{tag}", "object": ref}
                    for tag, ref in self.refs.items()
                    if tag.startswith(match)
                ],
            )
        elif rest.startswith("tags/"):
            self._reply(200, {"object": self.tags[rest.removeprefix("tags/")]})
        else:
            self._reply(404)

    def do_POST(self) -> None:
        if self.path == "/app/installations/456/access_tokens":
            self._reply(201, {"token": "installation-token"})
            return
        assert self.path == f"/repos/mitodl/{APP}/git/refs"
        assert self.headers["Authorization"] == "token installation-token"
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        tag = body["ref"].removeprefix("refs/tags/")
        _FakeGitHub.created.append(body)
        _FakeGitHub.refs[tag] = {"type": "commit", "sha": body["sha"]}
        if _FakeGitHub.race:
            # Another run created it first.
            _FakeGitHub.race = False
            self._reply(422, {"message": "Reference already exists"})
            return
        self._reply(201, {"ref": body["ref"]})


@pytest.fixture
def fake_github():
    _FakeGitHub.refs = {VERSION: {"type": "commit", "sha": COMMIT}}
    _FakeGitHub.tags = {}
    _FakeGitHub.race = False
    _FakeGitHub.created = []
    server = HTTPServer(("127.0.0.1", 0), _FakeGitHub)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()


@pytest.fixture(scope="module")
def private_key(tmp_path_factory) -> str:
    key = tmp_path_factory.mktemp("key") / "key.pem"
    subprocess.run(  # noqa: S603
        ["openssl", "genrsa", "-out", str(key), "2048"],  # noqa: S607
        check=True,
        capture_output=True,
    )
    return key.read_text()


def _promote(  # noqa: PLR0913
    tmp_path: Path,
    server: HTTPServer,
    private_key: str,
    version: str = VERSION,
    prefix: str = PRODUCTION_TAG_PREFIX,
    *,
    check_only: bool = False,
) -> subprocess.CompletedProcess[str]:
    (tmp_path / "version").write_text(version + "\n")
    env = {k: v for k, v in os.environ.items() if k != "CHECK_ONLY"}
    if check_only:
        env["CHECK_ONLY"] = "true"
    return subprocess.run(  # noqa: S603
        [sys.executable, "-c", _PROMOTE_TAG_SCRIPT],
        env={
            **env,
            "VERSION_FILE": str(tmp_path / "version"),
            "TAG_PREFIX": prefix,
            "GITHUB_REPOSITORY": f"mitodl/{APP}",
            "GITHUB_APP_ID": "123",
            "GITHUB_APP_INSTALLATION_ID": "456",
            "GITHUB_APP_PRIVATE_KEY": private_key,
            "GITHUB_API_URL": f"http://127.0.0.1:{server.server_address[1]}",
        },
        capture_output=True,
        text=True,
        check=False,
    )


def test_promote_creates_the_production_tag_at_the_release_tag(
    tmp_path, fake_github, private_key
):
    result = _promote(tmp_path, fake_github, private_key)

    assert result.returncode == 0, result.stderr
    assert _FakeGitHub.created == [
        {"ref": f"refs/tags/production/{VERSION}", "sha": COMMIT}
    ]


def test_promote_follows_an_annotated_release_tag(tmp_path, fake_github, private_key):
    _FakeGitHub.refs[VERSION] = {"type": "tag", "sha": TAG_OBJECT}
    _FakeGitHub.tags[TAG_OBJECT] = {"type": "commit", "sha": COMMIT}

    result = _promote(tmp_path, fake_github, private_key)

    assert result.returncode == 0, result.stderr
    assert _FakeGitHub.created[0]["sha"] == COMMIT


def test_a_rerun_changes_nothing(tmp_path, fake_github, private_key):
    _FakeGitHub.refs[f"production/{VERSION}"] = {"type": "commit", "sha": COMMIT}

    result = _promote(tmp_path, fake_github, private_key)

    assert result.returncode == 0, result.stderr
    assert "nothing to do" in result.stdout
    assert _FakeGitHub.created == []


def test_a_concurrent_create_of_the_same_tag_succeeds(
    tmp_path, fake_github, private_key
):
    _FakeGitHub.race = True

    result = _promote(tmp_path, fake_github, private_key)

    assert result.returncode == 0, result.stderr
    assert "nothing to do" in result.stdout


@pytest.mark.parametrize("own_tag", [False, True])
def test_a_version_older_than_production_is_refused(
    tmp_path, fake_github, private_key, own_tag
):
    """Re-running an old build must not report the old version as live."""
    _FakeGitHub.refs["production/2026.11.10.1"] = {"type": "commit", "sha": OTHER}
    # Not a release version; ignored rather than compared.
    _FakeGitHub.refs["production/latest"] = {"type": "commit", "sha": OTHER}
    if own_tag:
        _FakeGitHub.refs[f"production/{VERSION}"] = {"type": "commit", "sha": COMMIT}

    result = _promote(tmp_path, fake_github, private_key, check_only=True)

    assert result.returncode != 0
    assert "production/2026.11.10.1 is newer" in result.stderr
    assert _FakeGitHub.created == []


def test_an_older_production_tag_does_not_block_a_newer_version(
    tmp_path, fake_github, private_key
):
    # 2026.11.10.1 sorts before 2026.11.2.1 as a string but not as a version.
    _FakeGitHub.refs[VERSION.replace("2.1", "10.1")] = {"type": "commit", "sha": COMMIT}
    _FakeGitHub.refs[f"production/{VERSION}"] = {"type": "commit", "sha": OTHER}

    result = _promote(
        tmp_path, fake_github, private_key, version=VERSION.replace("2.1", "10.1")
    )

    assert result.returncode == 0, result.stderr
    assert _FakeGitHub.created == [
        {"ref": "refs/tags/production/2026.11.10.1", "sha": COMMIT}
    ]


def test_check_only_creates_nothing(tmp_path, fake_github, private_key):
    result = _promote(tmp_path, fake_github, private_key, check_only=True)

    assert result.returncode == 0, result.stderr
    assert "can be created" in result.stdout
    assert _FakeGitHub.created == []


def test_a_production_tag_is_never_moved(tmp_path, fake_github, private_key):
    _FakeGitHub.refs[f"production/{VERSION}"] = {"type": "commit", "sha": OTHER}

    result = _promote(tmp_path, fake_github, private_key)

    assert result.returncode != 0
    assert "never moved" in result.stderr
    assert _FakeGitHub.created == []


def test_an_abandoned_release_is_not_promoted(tmp_path, fake_github, private_key):
    del _FakeGitHub.refs[VERSION]

    result = _promote(tmp_path, fake_github, private_key)

    assert result.returncode != 0
    assert "nothing to promote" in result.stderr
    assert _FakeGitHub.created == []


@pytest.mark.parametrize(
    ("version", "prefix"),
    [("v1.165.0", PRODUCTION_TAG_PREFIX), (VERSION, ""), (VERSION, "../x/")],
)
def test_promote_refuses_a_bad_version_or_prefix(
    tmp_path, fake_github, private_key, version, prefix
):
    result = _promote(tmp_path, fake_github, private_key, version, prefix)

    assert result.returncode != 0
    assert "Not promoting" in result.stderr
    assert _FakeGitHub.created == []
