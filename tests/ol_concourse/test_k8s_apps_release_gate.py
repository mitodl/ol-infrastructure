"""Guard the Production job's check that the gate issue names what it deploys.

The release gate is a trigger: any closed issue matching ``Release <app>``
fires the Production job, which then deploys whatever last passed QA. These
tests pin the step that refuses a deploy the closed issue did not approve --
both where it sits in the generated plan and what the shipped script decides,
for the app version in the title and the ol-infrastructure commit in the body.
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
    _ASSERT_GATE_SCRIPT,
    _LABEL_GATE_SCRIPT,
    ASSERT_GATE_TASK_NAME,
    LABEL_GATE_TASK_NAME,
    build_app_pipeline,
)
from ol_concourse.pipelines.infrastructure.k8s_apps.scripts import (
    assert_gate_names_deploy,
    label_gate_issue,
)

APP = "ol-analytics-api"
REPO = f"mitodl/{APP}"
SHA = "a" * 40
OTHER_SHA = "b" * 40
GATE_NUMBER = 103


def _plan(app_name: str, job_suffix: str) -> list[dict[str, Any]]:
    pipeline = json.loads(build_app_pipeline(app_name).model_dump_json())
    (job,) = [j for j in pipeline["jobs"] if j["name"].endswith(job_suffix)]
    return job["plan"]


def _step_index(plan: list[dict[str, Any]], key: str, name: str) -> int:
    index = next((i for i, step in enumerate(plan) if step.get(key) == name), None)
    if index is None:
        steps = [next(iter(step.items())) for step in plan]
        msg = f"no {key!r} step named {name!r} in plan; steps are {steps}"
        raise AssertionError(msg)
    return index


def test_assertion_runs_after_the_gate_and_before_anything_deploys():
    """The check sits between the gate get and the first step that deploys."""
    plan = _plan(APP, "-production")

    gate = _step_index(plan, "get", f"{APP}-release-gate")
    check = _step_index(plan, "task", ASSERT_GATE_TASK_NAME)
    deployment_start = _step_index(plan, "put", f"{APP}-deployment-production")
    pulumi_up = _step_index(plan, "put", f"pulumi-ol-application-{APP}")

    assert gate < check < deployment_start < pulumi_up


def test_assertion_reads_the_version_the_job_deploys():
    """The check compares against image_tag's source, bound to what QA saw."""
    plan = _plan(APP, "-production")
    task = plan[_step_index(plan, "task", ASSERT_GATE_TASK_NAME)]
    release_get = plan[_step_index(plan, "get", f"{APP}-release")]

    assert task["config"]["params"]["VERSION_FILE"] == f"{APP}-release/version"
    assert release_get["passed"] == [f"deploy-ol-application-{APP}-qa"]


def test_assertion_reads_the_ol_infra_commit_the_job_deploys():
    """The check reads ol-infra through QA, before anything deploys.

    The job's own Pulumi code get comes after every dependency, so the check
    needs its own get of the same resource, bound to QA like the others so
    Concourse resolves both from the same QA build.
    """
    plan = _plan(APP, "-production")
    check = _step_index(plan, "task", ASSERT_GATE_TASK_NAME)
    revision = _step_index(plan, "get", f"ol-infra-{APP}-revision")
    code = _step_index(plan, "get", f"ol-infra-{APP}")
    task = plan[check]
    params = task["config"]["params"]

    assert revision < check < code
    assert plan[revision]["resource"] == plan[code]["get"]
    assert plan[revision]["passed"] == plan[code]["passed"]
    assert plan[revision]["passed"] == [f"deploy-ol-application-{APP}-qa"]
    assert not plan[revision]["trigger"]
    assert params["INFRA_REF_FILE"] == f"ol-infra-{APP}-revision/.git/ref"
    assert params["GITHUB_REPOSITORY"] == REPO
    assert params["GITHUB_APP_PRIVATE_KEY"] == "((github_app.release_bot_app_pem))"
    assert f"ol-infra-{APP}-revision" in {i["name"] for i in task["config"]["inputs"]}


def test_legacy_pipelines_are_untouched():
    """Legacy pipelines have no release gate, so they get no check."""
    pipeline = json.loads(build_app_pipeline("mit-learn").model_dump_json())
    assert ASSERT_GATE_TASK_NAME not in json.dumps(pipeline)


CLOSED_AT = "2026-10-15T19:43:38Z"


class _FakeGitHub(BaseHTTPRequestHandler):
    """The two GitHub calls the check makes: an installation token, one issue."""

    body: ClassVar[str | None] = ""
    closed: ClassVar[bool] = True
    closed_at: ClassVar[str | None] = CLOSED_AT
    # Replaces the whole GraphQL response when set.
    graphql_response: ClassVar[dict[str, Any] | None] = None
    last_edited_at: ClassVar[str | None] = "2026-10-15T19:33:53Z"
    calls: ClassVar[list[tuple[str, str]]] = []

    def log_message(self, *_args: Any) -> None:
        pass

    def _reply(self, body: Any) -> None:
        payload = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self) -> None:
        self.calls.append(("POST", self.path))
        if self.path == "/app/installations/456/access_tokens":
            assert self.headers["Authorization"].startswith("Bearer ")
            self._reply({"token": "installation-token"})
            return
        assert self.path == "/graphql"
        assert self.headers["Authorization"] == "bearer installation-token"
        assert self.headers["Content-Type"] == "application/json"
        length = int(self.headers["Content-Length"])
        variables = json.loads(self.rfile.read(length))["variables"]
        assert variables == {"owner": "mitodl", "name": APP, "number": GATE_NUMBER}
        if self.graphql_response is not None:
            self._reply(self.graphql_response)
            return
        issue = {
            "body": self.body,
            "closed": self.closed,
            "closedAt": self.closed_at,
            "lastEditedAt": self.last_edited_at,
        }
        self._reply({"data": {"repository": {"issue": issue}}})


@pytest.fixture
def fake_github():
    """Serve the fake GitHub API on a free local port."""
    _FakeGitHub.body = f"<!-- ol-infrastructure-revision: {SHA} -->\n## Release x\n"
    _FakeGitHub.closed = True
    _FakeGitHub.closed_at = CLOSED_AT
    _FakeGitHub.graphql_response = None
    _FakeGitHub.last_edited_at = "2026-10-15T19:33:53Z"
    _FakeGitHub.calls = []
    server = HTTPServer(("127.0.0.1", 0), _FakeGitHub)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()


@pytest.fixture(scope="module")
def private_key(tmp_path_factory) -> str:
    """Return a throwaway RSA key for the app JWT."""
    key = tmp_path_factory.mktemp("key") / "key.pem"
    subprocess.run(  # noqa: S603
        ["openssl", "genrsa", "-out", str(key), "2048"],  # noqa: S607
        check=True,
        capture_output=True,
    )
    return key.read_text()


def _run(  # noqa: PLR0913
    tmp_path: Path,
    server: HTTPServer,
    private_key: str,
    gate: dict[str, Any],
    version: str,
    revision: str = SHA,
) -> subprocess.CompletedProcess[str]:
    gate_file = tmp_path / "gh_issue.json"
    version_file = tmp_path / "version"
    ref_file = tmp_path / "ref"
    gate_file.write_text(json.dumps({"issue_number": str(GATE_NUMBER), **gate}))
    version_file.write_text(version)
    ref_file.write_text(revision + "\n")
    (tmp_path / "refusal").mkdir(exist_ok=True)
    return subprocess.run(  # noqa: S603
        [sys.executable, "-c", _ASSERT_GATE_SCRIPT],
        env={
            **os.environ,
            "REFUSAL_DIR": str(tmp_path / "refusal"),
            "APP_NAME": APP,
            "GATE_FILE": str(gate_file),
            "VERSION_FILE": str(version_file),
            "INFRA_REF_FILE": str(ref_file),
            "GITHUB_REPOSITORY": REPO,
            "GITHUB_APP_ID": "123",
            "GITHUB_APP_INSTALLATION_ID": "456",
            "GITHUB_APP_PRIVATE_KEY": private_key,
            "GITHUB_API_URL": f"http://127.0.0.1:{server.server_address[1]}",
        },
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.parametrize(
    ("title", "version", "allowed"),
    [
        # The release issue for the version being deployed.
        (f"Release {APP} 2026.9.22.1", "2026.9.22.1\n", True),
        # A stale issue for an older release, closed after a newer one was cut.
        (f"Release {APP} 2026.9.17.1", "2026.9.22.1\n", False),
        # An infrastructure-only gate at the live version.
        (f"Release {APP} infrastructure @ 2026.9.17.1", "2026.9.17.1", True),
        # An infrastructure-only gate while a newer release is in the job.
        (f"Release {APP} infrastructure @ 2026.9.17.1", "2026.9.22.1", False),
        # Bare prefix, no version: approves nothing in particular.
        (f"Release {APP}", "2026.9.22.1", False),
        # Another app sharing the prefix must not match.
        (f"Release {APP}-v2 2026.9.22.1", "2026.9.22.1", False),
    ],
)
def test_script_allows_only_the_approved_version(  # noqa: PLR0913
    tmp_path, fake_github, private_key, title, version, allowed
):
    """Only a title naming the deployed version lets the job proceed."""
    result = _run(tmp_path, fake_github, private_key, {"issue_title": title}, version)
    assert (result.returncode == 0) is allowed, result.stderr


def test_a_wrong_version_is_refused_without_calling_github(
    tmp_path, fake_github, private_key
):
    """The title check comes first and needs no API call."""
    gate = {"issue_title": f"Release {APP} 2026.9.17.1"}
    result = _run(tmp_path, fake_github, private_key, gate, "2026.9.22.1")

    assert result.returncode != 0
    assert _FakeGitHub.calls == []


@pytest.mark.parametrize(
    ("body", "allowed", "message"),
    [
        # The issue previewed the commit being deployed.
        (f"<!-- ol-infrastructure-revision: {SHA} -->\nbody", True, "approves"),
        # QA previewed a newer commit after this issue was closed, or failed
        # after posting it: the commit being deployed is not the one approved.
        (
            f"<!-- ol-infrastructure-revision: {OTHER_SHA} -->\nbody",
            False,
            f"approved the Production preview of {OTHER_SHA[:8]}",
        ),
        # An issue from before the marker existed, or one edited to drop it.
        ("## Release 2026.9.22.1\n", False, "does not say which"),
        (None, False, "does not say which"),
        # Two different revisions: the body no longer says which was approved.
        (
            f"<!-- ol-infrastructure-revision: {SHA} -->\n"
            f"<!-- ol-infrastructure-revision: {OTHER_SHA} -->",
            False,
            "does not say which",
        ),
    ],
)
def test_script_allows_only_the_previewed_ol_infra_commit(  # noqa: PLR0913
    tmp_path, fake_github, private_key, body, allowed, message
):
    """The body's revision marker must name the commit the job deploys."""
    _FakeGitHub.body = body
    gate = {"issue_title": f"Release {APP} 2026.9.22.1"}
    result = _run(tmp_path, fake_github, private_key, gate, "2026.9.22.1")

    assert (result.returncode == 0) is allowed, result.stderr
    assert message in (result.stdout + result.stderr)
    assert ("POST", "/graphql") in _FakeGitHub.calls


@pytest.mark.parametrize(
    ("closed", "closed_at", "last_edited_at", "allowed"),
    [
        # Edited before the close: the body is the one that was approved.
        (True, CLOSED_AT, "2026-10-15T19:33:53Z", True),
        # Never edited after creation.
        (True, CLOSED_AT, None, True),
        # A QA put found the just-closed issue through the lagging search
        # index and rewrote its body, marker included.
        (True, CLOSED_AT, "2026-10-15T19:43:50Z", False),
        # Same second as the close: cannot be ordered, so not trusted.
        (True, CLOSED_AT, CLOSED_AT, False),
        # Reopened since the gate saw it closed, whatever closedAt still says.
        (False, CLOSED_AT, "2026-10-15T19:33:53Z", False),
        (False, None, "2026-10-15T19:33:53Z", False),
    ],
)
def test_script_refuses_a_body_edited_after_the_close(  # noqa: PLR0913
    tmp_path, fake_github, private_key, closed, closed_at, last_edited_at, allowed
):
    """The marker counts only in the body the issue was closed with."""
    _FakeGitHub.closed = closed
    _FakeGitHub.closed_at = closed_at
    _FakeGitHub.last_edited_at = last_edited_at
    gate = {"issue_title": f"Release {APP} 2026.9.22.1"}
    result = _run(tmp_path, fake_github, private_key, gate, "2026.9.22.1")

    assert (result.returncode == 0) is allowed, result.stderr
    if not allowed:
        assert "edited at or after it was closed" in result.stderr


@pytest.mark.parametrize(
    ("response", "message"),
    [
        (
            {"data": {"repository": {"issue": None}}, "errors": [{"type": "x"}]},
            "GitHub GraphQL errors",
        ),
        ({"data": {"repository": {"issue": None}}}, "was not found"),
        ({"data": {"repository": None}}, "was not found"),
    ],
)
def test_script_fails_closed_when_the_issue_cannot_be_read(
    tmp_path, fake_github, private_key, response, message
):
    """A GraphQL error or a missing issue is a red build, not a deploy."""
    _FakeGitHub.graphql_response = response
    gate = {"issue_title": f"Release {APP} 2026.9.22.1"}
    result = _run(tmp_path, fake_github, private_key, gate, "2026.9.22.1")

    assert result.returncode != 0
    assert message in result.stderr
    assert "approves deploying" not in result.stdout


def test_script_refuses_a_ref_that_is_not_a_commit(tmp_path, fake_github, private_key):
    """A tag name or empty ref is not something an approval can name."""
    gate = {"issue_title": f"Release {APP} 2026.9.22.1"}
    result = _run(
        tmp_path, fake_github, private_key, gate, "2026.9.22.1", revision="v1.2.3"
    )

    assert result.returncode != 0
    assert "not a full commit SHA" in result.stderr


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        (f"Release {APP} 2026.9.22.1", "2026.9.22.1"),
        (f"  Release {APP} 2026.9.22.1\n", "2026.9.22.1"),
        (f"Release {APP} infrastructure @ 2026.9.17.1", "2026.9.17.1"),
        (f"Release {APP}", None),
        (f"Release {APP}-v2 2026.9.22.1", None),
        ("", None),
    ],
)
def test_approved_version_parses_only_the_accepted_shapes(title, expected):
    """The title parser returns the named version, or None for anything else."""
    assert assert_gate_names_deploy.approved_version(APP, title) == expected


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (f"<!-- ol-infrastructure-revision: {SHA} -->", SHA),
        # The same revision twice still names one commit.
        (
            f"<!-- ol-infrastructure-revision: {SHA} -->\nquoted: "
            f"<!-- ol-infrastructure-revision: {SHA} -->",
            SHA,
        ),
        (f"<!-- ol-infrastructure-revision: {SHA[:8]} -->", None),
        (f"<!-- ol-infrastructure-revision: {SHA.upper()} -->", None),
        ("", None),
        (None, None),
    ],
)
def test_approved_revision_parses_only_a_single_full_sha(body, expected):
    """The body parser returns one full SHA, or None for anything else."""
    assert assert_gate_names_deploy.approved_revision(body) == expected


def test_script_fails_closed_on_an_empty_gate_file(tmp_path):
    """download_version writes `{}` for an empty version; that must not pass."""
    gate_file = tmp_path / "gh_issue.json"
    gate_file.write_text("{}")
    (tmp_path / "version").write_text("2026.9.22.1")
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-c", _ASSERT_GATE_SCRIPT],
        env={
            **os.environ,
            "APP_NAME": APP,
            "GATE_FILE": str(gate_file),
            "VERSION_FILE": str(tmp_path / "version"),
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "Nothing was deployed" in result.stderr


def test_the_gate_is_checked_every_five_minutes():
    """An hourly gate check left a promotion waiting long enough to be refused.

    Production binds the newest ol-infrastructure revision that passed QA when
    it starts. Until a check records the closed issue, every ol-infrastructure
    merge that reaches QA moves that revision past the one the issue approved.
    """
    pipeline = json.loads(build_app_pipeline(APP).model_dump_json())
    (gate,) = [r for r in pipeline["resources"] if r["name"] == f"{APP}-release-gate"]

    assert gate["check_every"] == "5m"


def _gate_resource(app_name: str = APP) -> dict[str, Any]:
    pipeline = json.loads(build_app_pipeline(app_name).model_dump_json())
    (gate,) = [
        r for r in pipeline["resources"] if r["name"] == f"{app_name}-release-gate"
    ]
    return gate


def test_the_gate_labels_an_issue_it_consumes():
    """A consumed issue keeps its versioned title; the label marks it instead.

    The release-issue resource carries the same label name, because its
    close_with_labels looks for consumed issues by it.
    """
    pipeline = json.loads(build_app_pipeline(APP).model_dump_json())
    resources = {r["name"]: r for r in pipeline["resources"]}

    assert resources[f"{APP}-release-gate"]["source"]["consume_with_label"] == (
        "ops:consumed"
    )
    assert resources[f"{APP}-release-issue"]["source"]["consume_with_label"] == (
        "ops:consumed"
    )


def test_the_gate_skips_both_abandoned_label_names():
    """Renaming `abandoned` in a repository must never let the gate fire."""
    assert set(_gate_resource()["source"]["skip_if_labeled"]) == {
        "ops:abandoned",
        "abandoned",
    }


def test_a_refusal_labels_the_gate_issue():
    """A refused approval is recorded on the issue, not only as a red build.

    The hook is on the check task, not the job, so a deploy that fails after
    the check passed does not label the issue refused.
    """
    plan = _plan(APP, "-production")
    task = plan[_step_index(plan, "task", ASSERT_GATE_TASK_NAME)]
    hook = task["on_failure"]
    params = hook["config"]["params"]

    assert hook["task"] == LABEL_GATE_TASK_NAME
    assert params["ADD_LABELS"] == "ops:refused"
    # Only a refusal, which the check marks in its output, labels the issue.
    assert task["config"]["outputs"] == [{"name": "gate-refusal"}]
    assert task["config"]["params"]["REFUSAL_DIR"] == "gate-refusal"
    assert params["ONLY_IF_FILE"] == "gate-refusal/refused"
    assert "gate-refusal" in {i["name"] for i in hook["config"]["inputs"]}
    assert params["GATE_FILE"] == f"{APP}-release-gate/gh_issue.json"
    assert params["GITHUB_REPOSITORY"] == REPO
    assert hook["config"]["run"]["args"] == ["-c", _LABEL_GATE_SCRIPT]
    assert f"{APP}-release-gate" in {i["name"] for i in hook["config"]["inputs"]}
    pipeline = json.loads(build_app_pipeline(APP).model_dump_json())
    (job,) = [j for j in pipeline["jobs"] if j["name"].endswith("-production")]
    assert "label-gate-issue" not in json.dumps(job.get("on_failure"))


class _FakeLabelGitHub(BaseHTTPRequestHandler):
    """Token exchange, label creation, issue labelling and the re-read."""

    repo_labels: ClassVar[set[str]] = set()
    issue_labels: ClassVar[list[str]] = []
    # Labels another writer removes right after each add.
    strip_after_add: ClassVar[set[str]] = set()
    calls: ClassVar[list[tuple[str, str, Any]]] = []

    def log_message(self, *_args: Any) -> None:
        pass

    def _reply(self, status: int, body: Any) -> None:
        payload = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _body(self) -> Any:
        length = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(length)) if length else None

    def do_POST(self) -> None:
        body = self._body()
        self.calls.append(("POST", self.path, body))
        if self.path == "/app/installations/456/access_tokens":
            self._reply(201, {"token": "installation-token"})
            return
        assert self.headers["Authorization"] == "token installation-token"
        if self.path == f"/repos/{REPO}/labels":
            if body["name"] in self.repo_labels:
                self._reply(422, {"message": "already_exists"})
                return
            self.repo_labels.add(body["name"])
            self._reply(201, body)
            return
        assert self.path == f"/repos/{REPO}/issues/{GATE_NUMBER}/labels"
        for name in body["labels"]:
            if name not in self.issue_labels:
                self.issue_labels.append(name)
        for name in self.strip_after_add:
            if name in self.issue_labels:
                self.issue_labels.remove(name)
        self._reply(200, [{"name": n} for n in self.issue_labels])

    def do_GET(self) -> None:
        self.calls.append(("GET", self.path, None))
        assert self.path == f"/repos/{REPO}/issues/{GATE_NUMBER}"
        self._reply(200, {"labels": [{"name": n} for n in self.issue_labels]})

    def do_DELETE(self) -> None:
        msg = "the label script must never remove a label"
        raise AssertionError(msg)


@pytest.fixture
def fake_label_github():
    """Serve the fake GitHub API for the label script on a free local port."""
    _FakeLabelGitHub.repo_labels = set()
    _FakeLabelGitHub.issue_labels = ["ops:release", "ops:consumed"]
    _FakeLabelGitHub.strip_after_add = set()
    _FakeLabelGitHub.calls = []
    server = HTTPServer(("127.0.0.1", 0), _FakeLabelGitHub)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()


def _run_label(
    tmp_path: Path,
    server: HTTPServer,
    private_key: str,
    gate: dict[str, Any],
    labels: str = "ops:refused",
) -> subprocess.CompletedProcess[str]:
    gate_file = tmp_path / "gh_issue.json"
    gate_file.write_text(json.dumps(gate))
    return subprocess.run(  # noqa: S603
        [sys.executable, "-c", _LABEL_GATE_SCRIPT],
        env={
            **os.environ,
            "GATE_FILE": str(gate_file),
            "ADD_LABELS": labels,
            "GITHUB_REPOSITORY": REPO,
            "GITHUB_APP_ID": "123",
            "GITHUB_APP_INSTALLATION_ID": "456",
            "GITHUB_APP_PRIVATE_KEY": private_key,
            "GITHUB_API_URL": f"http://127.0.0.1:{server.server_address[1]}",
        },
        capture_output=True,
        text=True,
        check=False,
    )


def test_label_script_adds_the_label_and_keeps_the_others(
    tmp_path, fake_label_github, private_key
):
    """Additive only: the consumed label stays alongside the refusal."""
    result = _run_label(
        tmp_path, fake_label_github, private_key, {"issue_number": str(GATE_NUMBER)}
    )

    assert result.returncode == 0, result.stderr
    assert _FakeLabelGitHub.issue_labels == [
        "ops:release",
        "ops:consumed",
        "ops:refused",
    ]
    calls = [(m, p) for m, p, _ in _FakeLabelGitHub.calls]
    # The label exists before the issue is labelled with it.
    assert calls.index(("POST", f"/repos/{REPO}/labels")) < calls.index(
        ("POST", f"/repos/{REPO}/issues/{GATE_NUMBER}/labels")
    )
    assert ("GET", f"/repos/{REPO}/issues/{GATE_NUMBER}") in calls


def test_label_script_tolerates_an_existing_label(
    tmp_path, fake_label_github, private_key
):
    _FakeLabelGitHub.repo_labels = {"ops:refused"}
    _FakeLabelGitHub.issue_labels = ["ops:consumed", "ops:refused"]
    result = _run_label(
        tmp_path, fake_label_github, private_key, {"issue_number": str(GATE_NUMBER)}
    )

    assert result.returncode == 0, result.stderr
    assert _FakeLabelGitHub.issue_labels == ["ops:consumed", "ops:refused"]


def test_label_script_fails_when_the_label_keeps_disappearing(
    tmp_path, fake_label_github, private_key
):
    _FakeLabelGitHub.strip_after_add = {"ops:refused"}
    result = _run_label(
        tmp_path, fake_label_github, private_key, {"issue_number": str(GATE_NUMBER)}
    )

    assert result.returncode != 0
    assert "keep disappearing" in result.stderr
    adds = [
        c
        for c in _FakeLabelGitHub.calls
        if c[:2] == ("POST", f"/repos/{REPO}/issues/{GATE_NUMBER}/labels")
    ]
    assert len(adds) == 3


@pytest.mark.parametrize("gate", [{}, {"issue_number": "0"}])
def test_label_script_refuses_a_gate_file_naming_no_issue(
    tmp_path, fake_label_github, private_key, gate
):
    result = _run_label(tmp_path, fake_label_github, private_key, gate)

    assert result.returncode != 0
    assert "names no issue" in result.stderr
    assert _FakeLabelGitHub.calls == []


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("ops:refused", ["ops:refused"]),
        (" ops:refused , ops:x ,ops:refused,", ["ops:refused", "ops:x"]),
        ("", []),
    ],
)
def test_parse_labels(raw, expected):
    assert label_gate_issue.parse_labels(raw) == expected


@pytest.mark.parametrize(
    ("title", "body"),
    [
        # The issue approved another version.
        (f"Release {APP} 2026.9.17.1", f"<!-- ol-infrastructure-revision: {SHA} -->"),
        # The issue approved another ol-infrastructure commit.
        (
            f"Release {APP} 2026.9.22.1",
            f"<!-- ol-infrastructure-revision: {OTHER_SHA} -->",
        ),
        # The issue does not say which commit it approved.
        (f"Release {APP} 2026.9.22.1", "## Release 2026.9.22.1"),
        # The title names no version.
        (f"Release {APP}", f"<!-- ol-infrastructure-revision: {SHA} -->"),
        # The body was edited after the close.
        (f"Release {APP} 2026.9.22.1", None),
    ],
)
def test_a_refusal_leaves_the_marker_the_label_hook_needs(
    tmp_path, fake_github, private_key, title, body
):
    if body is None:
        _FakeGitHub.last_edited_at = "2026-10-15T19:43:50Z"
    else:
        _FakeGitHub.body = body
    result = _run(
        tmp_path, fake_github, private_key, {"issue_title": title}, "2026.9.22.1"
    )

    assert result.returncode != 0
    marker = tmp_path / "refusal" / "refused"
    assert marker.read_text().startswith("Refusing to deploy")


@pytest.mark.parametrize(
    ("response", "revision"),
    [
        # GitHub could not be read: not a refusal.
        ({"data": {"repository": {"issue": None}}, "errors": [{"type": "x"}]}, SHA),
        # A bad ref is a pipeline problem, not the approval's.
        (None, "v1.2.3"),
    ],
)
def test_a_failure_that_is_not_a_refusal_leaves_no_marker(
    tmp_path, fake_github, private_key, response, revision
):
    """An outage must not label an approved issue refused; a re-run deploys it."""
    _FakeGitHub.graphql_response = response
    gate = {"issue_title": f"Release {APP} 2026.9.22.1"}
    result = _run(
        tmp_path, fake_github, private_key, gate, "2026.9.22.1", revision=revision
    )

    assert result.returncode != 0
    assert not (tmp_path / "refusal" / "refused").exists()


def test_label_script_does_nothing_without_the_refusal_marker(
    tmp_path, fake_label_github, private_key
):
    gate_file = tmp_path / "gh_issue.json"
    gate_file.write_text(json.dumps({"issue_number": str(GATE_NUMBER)}))
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-c", _LABEL_GATE_SCRIPT],
        env={
            **os.environ,
            "GATE_FILE": str(gate_file),
            "ONLY_IF_FILE": str(tmp_path / "gate-refusal" / "refused"),
            "ADD_LABELS": "ops:refused",
            "GITHUB_REPOSITORY": REPO,
            "GITHUB_APP_ID": "123",
            "GITHUB_APP_INSTALLATION_ID": "456",
            "GITHUB_APP_PRIVATE_KEY": private_key,
            "GITHUB_API_URL": f"http://127.0.0.1:{fake_label_github.server_address[1]}",
        },
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert _FakeLabelGitHub.calls == []
    assert _FakeLabelGitHub.issue_labels == ["ops:release", "ops:consumed"]
