"""Guard the QA job's supersede step and the script behind it.

After the QA job posts the release issue that describes what Production would
ship, every other open gate issue for the app is closed and labelled
``abandoned`` so the release gate cannot fire on it. These tests pin where the
step sits, which issues it touches, and the GitHub calls it makes.
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

from ol_concourse.pipelines.infrastructure.k8s_apps.pipeline import (
    _SUPERSEDE_SCRIPT,
    SUPERSEDE_TASK_NAME,
    _build_release_resource_app_pipeline,
    build_app_pipeline,
    pipeline_params,
)
from ol_concourse.pipelines.infrastructure.k8s_apps.scripts import (
    assert_gate_names_deploy,
    supersede_gate_issues,
)

APP = "ol-analytics-api"
REPO = f"mitodl/{APP}"
DROP = "not_planned"
DONE = "completed"


def _job(app_name: str, job_suffix: str) -> dict[str, Any]:
    pipeline = json.loads(build_app_pipeline(app_name).model_dump_json())
    (job,) = [j for j in pipeline["jobs"] if j["name"].endswith(job_suffix)]
    return job


def _nextjs_qa_plan() -> list[dict[str, Any]]:
    """mit-learn-nextjs's QA plan as it will be once it moves to the new workflow.

    Built directly rather than through ``build_app_pipeline``, which still
    routes it to the legacy shape. It is the one app with both a Fastly purge
    and a repository that is not ``mitodl/<app>``.
    """
    pipeline = _build_release_resource_app_pipeline(
        "mit-learn-nextjs", pipeline_params["mit-learn-nextjs"]
    )
    jobs = json.loads(pipeline.model_dump_json())["jobs"]
    (job,) = [j for j in jobs if j["name"].endswith("-qa")]
    return job["plan"]


def _issue(number: int, title: str, **extra: Any) -> dict[str, Any]:
    return {"number": number, "title": title, "labels": [], **extra}


def test_supersede_runs_last_in_qa_after_the_issue_put():
    """It needs the posted issue, and must not mask the RC finish or a purge."""
    plan = _job(APP, "-qa")["plan"]
    names = [step.get("task") or step.get("put") for step in plan]

    assert names[-1] == SUPERSEDE_TASK_NAME
    assert names.index(f"{APP}-release-issue") < names.index(SUPERSEDE_TASK_NAME)
    assert not any("try" in step for step in plan)


def test_supersede_runs_after_the_fastly_purge():
    """mit-learn-nextjs purges Fastly in QA; supersede still comes last."""
    plan = _nextjs_qa_plan()
    assert any("fastly" in (step.get("put") or "") for step in plan)
    assert plan[-1]["task"] == SUPERSEDE_TASK_NAME


def test_supersede_task_is_wired_to_the_posted_issue_and_the_app():
    """It reads the put's issue, the promotion decision, and the release App."""
    plan = _job(APP, "-qa")["plan"]
    (task,) = [step for step in plan if step.get("task") == SUPERSEDE_TASK_NAME]
    params = task["config"]["params"]

    assert task["attempts"] == 3
    assert params["APP_NAME"] == APP
    assert params["GITHUB_REPOSITORY"] == REPO
    assert params["ISSUE_FILE"] == f"{APP}-release-issue/gh_issue.json"
    assert params["PROMOTION_DIR"] == "promotion"
    assert params["GITHUB_APP_PRIVATE_KEY"] == "((github_app.release_bot_app_pem))"
    assert {i["name"] for i in task["config"]["inputs"]} == {
        "promotion",
        f"{APP}-release-issue",
    }


def test_supersede_uses_the_app_registry_repo():
    """mit-learn-nextjs lives in mitodl/mit-learn, not mitodl/mit-learn-nextjs."""
    (task,) = [s for s in _nextjs_qa_plan() if s.get("task") == SUPERSEDE_TASK_NAME]
    assert task["config"]["params"]["GITHUB_REPOSITORY"] == "mitodl/mit-learn"


def test_production_and_legacy_pipelines_do_not_supersede():
    """Only QA posts gate issues, and legacy pipelines have none."""
    production = json.dumps(_job(APP, "-production"))
    legacy = build_app_pipeline("mitxonline").model_dump_json()

    assert SUPERSEDE_TASK_NAME not in production
    assert SUPERSEDE_TASK_NAME not in legacy


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        (f"Release {APP} 2026.10.8.1", True),
        (f"Release {APP} infrastructure @ 2026.10.7.1", True),
        # The tombstone the gate's get leaves on a consumed issue.
        (f"[CONSUMED #12]Release {APP} 2026.10.7.1", False),
        # The bare title names no version, so no deploy can match it.
        (f"Release {APP}", False),
        (f"Release {APP} 2026.10.8.1 and more", False),
        ("Something else entirely", False),
    ],
)
def test_is_gate_title(title, expected):
    """Only whole gate titles count."""
    assert supersede_gate_issues.is_gate_title(APP, title) is expected


@pytest.mark.parametrize(
    "title",
    [
        "Release mit-learn 2026.10.8.1",
        "Release mit-learn infrastructure @ 2026.10.8.1",
        "Release mit-learn-nextjs 2026.10.8.1",
        "Release mit-learn-nextjs infrastructure @ 2026.10.8.1",
        "Release mit-learn",
        "[CONSUMED #3]Release mit-learn 2026.10.8.1",
    ],
)
def test_gate_title_agrees_with_the_production_assertion(title):
    """Whatever Production would accept for an app, supersede must recognise."""
    for app in ("mit-learn", "mit-learn-nextjs"):
        accepted = assert_gate_names_deploy.approved_version(app, title) is not None
        assert supersede_gate_issues.is_gate_title(app, title) is accepted


def test_a_shared_repository_keeps_the_other_apps_issues():
    """mit-learn and mit-learn-nextjs share a repo; each supersedes only its own."""
    issues = [
        _issue(1, "Release mit-learn 2026.10.1.1"),
        _issue(2, "Release mit-learn-nextjs 2026.10.1.1"),
        _issue(3, "Release mit-learn 2026.10.2.1"),
    ]
    stale = supersede_gate_issues.stale_issues("mit-learn", issues, keep_number=3)
    assert [issue["number"] for issue in stale] == [1]


def test_stale_issues_keeps_the_posted_issue_and_skips_pull_requests():
    """Everything but the posted issue goes, oldest first; a PR never does."""
    issues = [
        _issue(96, f"Release {APP} 2026.10.7.1"),
        _issue(97, f"Release {APP} 2026.10.8.1"),
        _issue(83, f"Release {APP} 2026.9.30.1"),
        _issue(90, f"Release {APP} 2026.9.1.1", pull_request={"url": "x"}),
        _issue(91, "Unrelated bug"),
    ]
    stale = supersede_gate_issues.stale_issues(APP, issues, keep_number=97)
    assert [issue["number"] for issue in stale] == [83, 96]


def test_closed_issues_are_stale_only_when_discarded_and_unlabelled():
    """A not-planned close is finished; a completed close is an approval."""
    issues = [
        _issue(94, f"Release {APP} 2026.10.6.1", state="closed", state_reason=DROP),
        _issue(
            95,
            f"Release {APP} 2026.10.5.1",
            state="closed",
            state_reason=DROP,
            labels=[{"name": "abandoned"}],
        ),
        _issue(
            92,
            f"[CONSUMED #7]Release {APP} 2026.10.4.1",
            state="closed",
            state_reason=DROP,
        ),
        # A reviewer's approval, even though #97 now describes the deploy.
        _issue(93, f"Release {APP} 2026.10.8.1", state="closed", state_reason=DONE),
        # Listed by both the open and the closed query: handled once.
        _issue(96, f"Release {APP} 2026.10.7.1", state="open"),
        _issue(96, f"Release {APP} 2026.10.7.1", state="open"),
    ]
    stale = supersede_gate_issues.stale_issues(APP, issues, keep_number=97)
    assert [issue["number"] for issue in stale] == [94, 96]


def test_nothing_to_approve_supersedes_every_gate_issue():
    """With no issue posted, no open gate issue describes what would deploy."""
    issues = [
        _issue(96, f"Release {APP} 2026.10.7.1"),
        _issue(98, f"Release {APP} infrastructure @ 2026.10.7.1"),
    ]
    stale = supersede_gate_issues.stale_issues(APP, issues, keep_number=None)
    assert [issue["number"] for issue in stale] == [96, 98]


def test_kept_issue_refuses_a_put_that_posted_nothing(tmp_path):
    """No skip marker but no issue: superseding all would close the real one."""
    (tmp_path / "promotion").mkdir()
    issue_file = tmp_path / "gh_issue.json"
    issue_file.write_text(json.dumps({"issue_number": "0", "issue_title": ""}))

    with pytest.raises(ValueError, match="reported no issue"):
        supersede_gate_issues.kept_issue(tmp_path / "promotion", issue_file)


class _FakeGitHub(BaseHTTPRequestHandler):
    """Just enough of the GitHub REST API, with two pages of open issues."""

    issues: ClassVar[dict[int, dict[str, Any]]] = {}
    calls: ClassVar[list[tuple[str, str, Any]]] = []
    # Issue numbers whose labels another writer replaces right after the close.
    strip_after_close: ClassVar[set[int]] = set()

    def log_message(self, *_args: Any) -> None:
        pass

    def _reply(self, body: Any, headers: dict[str, str] | None = None) -> None:
        payload = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _body(self) -> Any:
        length = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(length)) if length else None

    def do_POST(self) -> None:
        body = self._body()
        self.calls.append(("POST", self.path, body))
        if self.path.startswith("/app/installations/"):
            assert self.headers["Authorization"].startswith("Bearer ")
            self._reply({"token": "installation-token"})
            return
        assert self.headers["Authorization"] == "token installation-token"
        if self.path.endswith("/labels"):
            issue = self.issues[int(self.path.split("/")[-2])]
            names = {label["name"] for label in issue["labels"]} | set(body["labels"])
            issue["labels"] = [{"name": name} for name in sorted(names)]
        self._reply({})

    def do_PATCH(self) -> None:
        body = self._body()
        self.calls.append(("PATCH", self.path, body))
        number = int(self.path.rsplit("/", 1)[-1])
        issue = self.issues[number]
        issue["state"] = body["state"]
        issue["state_reason"] = body["state_reason"]
        issue["labels"] = [{"name": name} for name in body["labels"]]
        if number in self.strip_after_close:
            issue["labels"] = []
        self._reply(issue)

    def do_GET(self) -> None:
        self.calls.append(("GET", self.path, None))
        open_issues = [i for i in self.issues.values() if i["state"] == "open"]
        if "state=closed" in self.path:
            assert "since=" in self.path
            self._reply([i for i in self.issues.values() if i["state"] == "closed"])
        elif "page=2" in self.path:
            self._reply(open_issues[1:])
        elif "/issues?" in self.path:
            # Lowercase, as HTTP/2 delivers it.
            next_url = f"http://{self.headers['Host']}/repos/{REPO}/issues?page=2"
            self._reply(open_issues[:1], {"link": f'<{next_url}>; rel="next"'})
        else:
            self._reply(self.issues[int(self.path.rsplit("/", 1)[-1])])


@pytest.fixture
def fake_github():
    """Serve the fake GitHub API on a free local port."""
    _FakeGitHub.calls = []
    _FakeGitHub.strip_after_close = set()
    _FakeGitHub.issues = {
        83: _issue(83, f"Release {APP} 2026.9.30.1", state="open"),
        96: _issue(
            96,
            f"Release {APP} 2026.10.7.1",
            state="open",
            labels=[{"name": "release"}],
        ),
        97: _issue(97, f"Release {APP} 2026.10.8.1", state="open"),
        99: _issue(99, "Unrelated bug", state="open"),
        # Discarded by a person before QA ran; the gate has not consumed it.
        94: _issue(94, f"Release {APP} 2026.10.6.1", state="closed", state_reason=DROP),
        # Approved by a reviewer; a later QA run then posted #97.
        95: _issue(95, f"Release {APP} 2026.10.8.1", state="closed", state_reason=DONE),
        90: _issue(
            90,
            f"[CONSUMED #5]Release {APP} 2026.9.1.1",
            state="closed",
            state_reason=DROP,
        ),
    }
    server = HTTPServer(("127.0.0.1", 0), _FakeGitHub)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()


@pytest.fixture
def private_key(tmp_path) -> str:
    """Return a throwaway RSA key for the app JWT."""
    key = tmp_path / "key.pem"
    subprocess.run(  # noqa: S603
        ["openssl", "genrsa", "-out", str(key), "2048"],  # noqa: S607
        check=True,
        capture_output=True,
    )
    return key.read_text()


def _run(
    tmp_path: Path,
    server: HTTPServer,
    private_key: str,
    *,
    posted: int,
    skip: bool = False,
) -> subprocess.CompletedProcess[str]:
    promotion = tmp_path / "promotion"
    promotion.mkdir()
    (promotion / "title").write_text(f"Release {APP} 2026.10.8.1")
    if skip:
        (promotion / "skip").write_text("nothing to approve\n")
    issue_file = tmp_path / "gh_issue.json"
    issue_file.write_text(json.dumps({"issue_number": str(posted), "issue_title": "x"}))
    api_url = f"http://127.0.0.1:{server.server_address[1]}"
    return subprocess.run(  # noqa: S603
        [sys.executable, "-c", _SUPERSEDE_SCRIPT],
        env={
            **os.environ,
            "APP_NAME": APP,
            "GITHUB_REPOSITORY": REPO,
            "PROMOTION_DIR": str(promotion),
            "ISSUE_FILE": str(issue_file),
            "GITHUB_APP_ID": "123",
            "GITHUB_APP_INSTALLATION_ID": "456",
            "GITHUB_APP_PRIVATE_KEY": private_key,
            "GITHUB_API_URL": api_url,
        },
        capture_output=True,
        text=True,
        check=False,
    )


def test_script_supersedes_stale_issues_end_to_end(tmp_path, fake_github, private_key):
    """It pages the open issues, then comments on, closes and labels the stale ones."""
    result = _run(tmp_path, fake_github, private_key, posted=97)

    assert result.returncode == 0, result.stderr
    issues = _FakeGitHub.issues
    assert issues[83]["state"] == issues[96]["state"] == "closed"
    assert {label["name"] for label in issues[94]["labels"]} == {"abandoned"}
    assert issues[90]["labels"] == issues[95]["labels"] == []
    assert {label["name"] for label in issues[96]["labels"]} == {
        "release",
        "abandoned",
    }
    assert issues[97]["state"] == issues[99]["state"] == "open"
    comments = [
        (path, body["body"])
        for method, path, body in _FakeGitHub.calls
        if method == "POST" and path.endswith("/comments")
    ]
    assert [path for path, _ in comments] == [
        f"/repos/{REPO}/issues/83/comments",
        f"/repos/{REPO}/issues/94/comments",
        f"/repos/{REPO}/issues/96/comments",
    ]
    assert "Superseded by #97" in comments[0][1]
    assert "Closed and labelled" in comments[0][1]
    assert "Labelled `abandoned`" in comments[1][1]
    # Each comment lands before its close, so a closed issue always says why.
    methods = [(m, p) for m, p, _ in _FakeGitHub.calls if "/issues/83" in p]
    assert methods.index(("POST", f"/repos/{REPO}/issues/83/comments")) < (
        methods.index(("PATCH", f"/repos/{REPO}/issues/83"))
    )


def test_script_with_nothing_to_approve_closes_every_gate_issue(
    tmp_path, fake_github, private_key
):
    """A skipped put keeps nothing open, and says why."""
    result = _run(tmp_path, fake_github, private_key, posted=0, skip=True)

    assert result.returncode == 0, result.stderr
    issues = _FakeGitHub.issues
    assert [n for n, i in sorted(issues.items()) if i["state"] == "open"] == [99]
    comment = next(
        body["body"]
        for method, path, body in _FakeGitHub.calls
        if method == "POST" and path.endswith("/comments")
    )
    assert "nothing for this issue to approve" in comment


def test_script_refuses_before_calling_github(tmp_path, fake_github, private_key):
    """A put that should have posted an issue but names none touches nothing."""
    result = _run(tmp_path, fake_github, private_key, posted=0)

    assert result.returncode != 0
    assert "Refusing to supersede" in result.stderr
    assert _FakeGitHub.calls == []


def test_script_puts_back_a_label_another_writer_removed(
    tmp_path, fake_github, private_key
):
    """The label is what stops the gate, so it is confirmed after the close."""
    _FakeGitHub.strip_after_close = {96}
    result = _run(tmp_path, fake_github, private_key, posted=97)

    assert result.returncode == 0, result.stderr
    assert {label["name"] for label in _FakeGitHub.issues[96]["labels"]} == {
        "abandoned"
    }


def test_a_retry_finds_an_issue_closed_without_its_label(
    tmp_path, fake_github, private_key
):
    """An earlier attempt that closed but could not label is finished later."""
    _FakeGitHub.issues[96]["state"] = "closed"
    _FakeGitHub.issues[96]["state_reason"] = DROP
    result = _run(tmp_path, fake_github, private_key, posted=97)

    assert result.returncode == 0, result.stderr
    assert "abandoned" in {label["name"] for label in _FakeGitHub.issues[96]["labels"]}
