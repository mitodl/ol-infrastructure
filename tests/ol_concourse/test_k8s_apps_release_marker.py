"""Guard the Production release marker and the script behind it.

Each Production deploy of a k8s app posts a Grafana annotation spanning the
rollout, unless the version deployed is the one the last marker names. These
tests pin where the two steps sit in both release workflows, what the marker
task reads, and the Grafana calls the script makes.
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, ClassVar

import pytest

from ol_concourse.pipelines.infrastructure.k8s_apps.pipeline import (
    GRAFANA_ANNOTATIONS_TOKEN,
    GRAFANA_ANNOTATIONS_URL,
    RELEASE_MARKER_START_TASK_NAME,
    RELEASE_MARKER_TASK_NAME,
    build_app_pipeline,
)
from ol_concourse.pipelines.infrastructure.k8s_apps.scripts import (
    grafana_release_marker,
)
from ol_infrastructure.infrastructure.grafana_alerting.release_markers import (
    CONCOURSE_VAULT_PATH,
    FIXED_ROLES,
)

# One app per workflow: ol-analytics-api is on the release-resource workflow,
# mit-learn-nextjs on the legacy release-branch one.
APPS = ["ol-analytics-api", "mit-learn-nextjs"]
INFRA_SHA = "3f2a9c1d4e5b6a7c8d9e0f1a2b3c4d5e6f7a8b9c"  # pragma: allowlist secret

# What Grafana's fixed roles grant on organization annotations, and what each
# call the script makes requires:
# https://github.com/grafana/grafana/blob/c762526fbe957bbf880894235e1035865c36a966/pkg/api/accesscontrol.go#L286-L319
# https://github.com/grafana/grafana/blob/c762526fbe957bbf880894235e1035865c36a966/pkg/api/api.go#L519-L523
ROLE_ACTIONS = {
    "fixed:annotations:reader": {"annotations:read"},
    "fixed:annotations:writer": {
        "annotations:create",
        "annotations:delete",
        "annotations:write",
    },
}
REQUIRED_ACTION = {"GET": "annotations:read", "POST": "annotations:create"}


def _plan(app_name: str, job_suffix: str) -> list[dict[str, Any]]:
    pipeline = json.loads(build_app_pipeline(app_name).model_dump_json())
    (job,) = [j for j in pipeline["jobs"] if j["name"].endswith(job_suffix)]
    return job["plan"]


def _step_name(step: dict[str, Any]) -> str:
    if "try" in step:
        return step["try"]["task"]
    return step.get("task") or step.get("put") or step.get("get") or ""


@pytest.mark.parametrize("app_name", APPS)
def test_production_marks_the_rollout_around_the_pulumi_put(app_name):
    """The start is taken before the Pulumi put and the marker runs right after it.

    Right after, so a later post-step failing cannot cost a live release its
    marker. Both are in a ``try``, so neither can fail a deploy.
    """
    plan = _plan(app_name, "-production")
    names = [_step_name(step) for step in plan]
    pulumi_put = names.index(f"pulumi-ol-application-{app_name}")
    start = names.index(RELEASE_MARKER_START_TASK_NAME)

    assert start < pulumi_put
    assert names[pulumi_put + 1] == RELEASE_MARKER_TASK_NAME
    assert "try" in plan[start]
    assert "try" in plan[pulumi_put + 1]


@pytest.mark.parametrize("app_name", APPS)
def test_qa_posts_no_marker(app_name):
    """QA reports to a different Grafana, which the token does not reach."""
    names = [_step_name(step) for step in _plan(app_name, "-qa")]

    assert RELEASE_MARKER_START_TASK_NAME not in names
    assert RELEASE_MARKER_TASK_NAME not in names


@pytest.mark.parametrize("app_name", APPS)
def test_marker_reads_the_deployed_version_and_infra_commit(app_name):
    """Both come from the job itself: the image_tag var and its ol-infra get."""
    (task,) = [
        step["try"]
        for step in _plan(app_name, "-production")
        if _step_name(step) == RELEASE_MARKER_TASK_NAME
    ]
    params = task["config"]["params"]

    assert params["APP_NAME"] == app_name
    assert params["ENVIRONMENT"] == "production"
    assert params["VERSION"] == "((.:image_tag))"
    assert params["INFRA_REF_FILE"] == f"ol-infra-{app_name}/.git/ref"
    assert params["GRAFANA_URL"] == GRAFANA_ANNOTATIONS_URL
    assert params["GRAFANA_TOKEN"] == GRAFANA_ANNOTATIONS_TOKEN
    assert {i["name"] for i in task["config"]["inputs"]} == {
        "release-marker",
        f"ol-infra-{app_name}",
    }


def test_concourse_reads_the_vault_path_the_grafana_stack_writes():
    """The infrastructure team's ((grafana_annotations.*)) is that Vault path."""
    var_name = GRAFANA_ANNOTATIONS_TOKEN.removeprefix("((").split(".")[0]

    assert f"secret-concourse/infrastructure/{var_name}" == CONCOURSE_VAULT_PATH


class _Grafana(BaseHTTPRequestHandler):
    granted: ClassVar[set[str]] = set()
    existing: ClassVar[list[dict[str, Any]]] = []
    requests: ClassVar[list[dict[str, Any]]] = []

    def do_GET(self):
        self._record(None)
        self._reply_if_permitted(self.existing)

    def do_POST(self):
        self._record(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
        self._reply_if_permitted({"id": 1, "message": "Annotation added"})

    def _reply_if_permitted(self, payload: Any) -> None:
        if REQUIRED_ACTION[self.command] in self.granted:
            self._reply(200, payload)
        else:
            self._reply(403, {"message": "Access denied"})

    def _record(self, body: Any) -> None:
        self.requests.append(
            {
                "method": self.command,
                "path": self.path,
                "auth": self.headers["Authorization"],
                "body": body,
            }
        )

    def _reply(self, status: int, payload: Any) -> None:
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *_args: Any) -> None:
        pass


@pytest.fixture
def grafana(monkeypatch, tmp_path: Path):
    """Start a fake Grafana and point the marker script's environment at it.

    It permits only what the service account's roles grant, so a call the
    script makes that those roles do not cover fails here with the 403 the
    real Grafana would return.
    """
    _Grafana.granted = set().union(*(ROLE_ACTIONS[role] for role in FIXED_ROLES))
    _Grafana.existing = []
    _Grafana.requests = []
    server = HTTPServer(("127.0.0.1", 0), _Grafana)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    started_at = tmp_path / "started_at"
    started_at.write_text("1794000000\n")
    infra_ref = tmp_path / "ref"
    infra_ref.write_text(f"{INFRA_SHA}\n")
    monkeypatch.setenv("APP_NAME", "mitxonline")
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("VERSION", "2026.11.9.1")
    monkeypatch.setenv("STARTED_AT_FILE", str(started_at))
    monkeypatch.setenv("INFRA_REF_FILE", str(infra_ref))
    monkeypatch.setenv("INFRA_REPO_URL", "https://github.com/mitodl/ol-infrastructure")
    # The trailing slash is deliberate: the POST path assertion below fails on
    # a doubled slash if the script stops stripping it.
    monkeypatch.setenv("GRAFANA_URL", f"http://127.0.0.1:{server.server_port}/")
    monkeypatch.setenv("GRAFANA_TOKEN", "test-token")
    yield _Grafana
    server.shutdown()


def test_a_run_that_deploys_the_marked_version_posts_nothing(grafana):
    """An ol-infrastructure-only deploy keeps the version the newest marker names."""
    grafana.existing = [
        {"time": 1_700_000_000_000, "tags": ["version:2026.11.2.1"]},
        {"time": 1_793_000_000_000, "tags": ["version:2026.11.9.1"]},
        {"time": 1_750_000_000_000, "tags": ["version:2026.11.5.1"]},
    ]

    grafana_release_marker.main()

    assert [request["method"] for request in grafana.requests] == ["GET"]
    assert "tags=mitxonline%2Fproduction%2Frelease" in grafana.requests[0]["path"]


@pytest.mark.parametrize(
    "existing",
    [[], [{"time": 1_700_000_000_000, "tags": ["version:2026.11.2.1"]}]],
    ids=["first-marker", "new-version"],
)
def test_a_release_posts_a_marker_spanning_the_rollout(grafana, existing):
    """An organization annotation from the recorded start to now, tagged to filter."""
    grafana.existing = existing

    grafana_release_marker.main()

    post = grafana.requests[-1]
    body = post["body"]
    assert (post["method"], post["path"]) == ("POST", "/api/annotations")
    assert post["auth"] == "Bearer test-token"
    assert body["time"] == 1_794_000_000_000
    assert body["timeEnd"] > body["time"]
    assert "dashboardUID" not in body
    assert set(body["tags"]) >= {
        "kind:release",
        "version:2026.11.9.1",
        f"infra:{INFRA_SHA[:9]}",
        "mitxonline/production/release",
    }
    assert (
        f"https://github.com/mitodl/ol-infrastructure/commit/{INFRA_SHA}"
        in body["text"]
    )
