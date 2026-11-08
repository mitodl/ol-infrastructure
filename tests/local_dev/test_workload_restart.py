"""workload-restart.py without a cluster: which files a restart copies, the
SIGHUP/SIGQUIT loop against a fake pod, and the beat warning it raises and
clears through a stubbed tilt.
"""

import argparse
import importlib.util
import os
import stat
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

REPO = Path(__file__).parents[2]
SCRIPT = REPO / "local-dev/scripts/workload-restart.py"


@pytest.fixture(scope="module")
def script():
    spec = importlib.util.spec_from_file_location("workload_restart", SCRIPT)
    assert spec
    assert spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _git(repo: Path, *args: str) -> None:
    # Independent of the developer's global config: no signing, no hooks.
    config = ["-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null"]
    subprocess.run(  # noqa: S603
        ["git", "-C", str(repo), *config, *args],  # noqa: S607
        check=True,
        capture_output=True,
    )


def test_the_copy_is_the_working_tree_as_git_sees_it(script, tmp_path):
    """Tracked and new files go; ignored files, .claude/ and files deleted from
    the tree (which the pod would otherwise get back from the index) do not.
    """
    repo = tmp_path / "app"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / ".gitignore").write_text("*.log\n")
    (repo / "tracked.py").write_text("")
    (repo / "gone.py").write_text("")
    _git(repo, "add", ".")
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init")
    (repo / "new.py").write_text("")
    (repo / "debug.log").write_text("")
    (repo / ".claude").mkdir()
    (repo / ".claude/worktree.txt").write_text("")
    (repo / "gone.py").unlink()

    assert script.checkout_files(repo) == [".gitignore", "new.py", "tracked.py"]


def test_the_celery_app_comes_from_the_container_command(script):
    assert (
        script.celery_app_from_command(["celery", "-A", "main.celery:app", "worker"])
        == "main.celery:app"
    )
    assert script.celery_app_from_command(["celery", "worker"]) is None


@pytest.fixture
def tilt_stub(tmp_path, monkeypatch):
    """Put a tilt on PATH that records its arguments and stdin."""
    log = tmp_path / "tilt.log"
    stub = tmp_path / "bin/tilt"
    stub.parent.mkdir()
    stub.write_text('#!/usr/bin/env bash\n{ echo "tilt $*"; cat; } >> "$TILT_LOG"\n')
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{stub.parent}:{os.environ['PATH']}")
    monkeypatch.setenv("TILT_LOG", str(log))
    return log


def test_a_stale_schedule_puts_a_button_on_the_beat_resource_that_recreates_the_stack(
    script, tilt_stub
):
    script.sync_beat_warning("x", "x-celery-beat", "x-app", stale=True)
    calls = tilt_stub.read_text()
    assert calls.startswith("tilt apply -f -\n")
    assert "componentID: x-celery-beat" in calls
    assert "tilt trigger x-app" in calls


def test_a_matching_schedule_removes_the_button(script, tilt_stub):
    script.sync_beat_warning("x", "x-celery-beat", "x-app", stale=False)
    assert tilt_stub.read_text() == (
        "tilt delete uibutton/x-beat-schedule-stale"
        " cmd/x-beat-schedule-stale-recreate --ignore-not-found\n"
    )


class FakePod:
    """A worker pod as the restart sees it, with a clock the test drives: the
    log, the pool PIDs, the restart count and whether pings are answered are
    what the stubbed kubectl calls report, and `events` maps a time to a
    change in them.
    """

    def __init__(self, script, monkeypatch):
        self.log = ""
        self.pool = {"11", "12"}
        self.restarts = 0
        self.running = True
        self.answers_pings = True
        self.deployment_exists = True
        self.signals: list[tuple[float, str]] = []
        self.copies: list[float] = []
        self.now = 0.0
        self.events: dict[float, Callable[[], None]] = {}
        self.on_signal: dict[str, Callable[[], None]] = {}
        monkeypatch.setattr(script, "log", lambda _message: None)
        monkeypatch.setattr(
            script,
            "container_status",
            lambda *_: {
                "restarts": self.restarts,
                "running": self.running,
                "last_reason": None,
            },
        )
        monkeypatch.setattr(script, "pool_children", lambda *_: set(self.pool))
        monkeypatch.setattr(script, "mark_log", self.mark)
        monkeypatch.setattr(script, "log_after", self.after)
        monkeypatch.setattr(script, "signal_pid1", self.signal)
        monkeypatch.setattr(script, "ping", lambda *_: self.answers_pings)
        monkeypatch.setattr(script, "time", self)
        # restart_now's surroundings: the Deployment, the waits and the copy.
        monkeypatch.setattr(script, "get_json", self.deployment)
        monkeypatch.setattr(script, "wait_for_app_row", self.app_applied)
        monkeypatch.setattr(script, "wait_for_rollout", lambda *_: None)
        monkeypatch.setattr(script, "running_pod", lambda *_: {"name": "x-worker"})
        monkeypatch.setattr(
            script, "copy_checkout", lambda *_: self.copies.append(self.now)
        )
        monkeypatch.setattr(script, "print_deployment", lambda *_: None)

    def app_applied(self, *_):
        self.deployment_exists = True

    def deployment(self, *_):
        if not self.deployment_exists:
            sys.exit("kubectl get deploy x-worker failed: NotFound")
        command = ["celery", "-A", "main", "worker"]
        container = {"name": "celery-worker", "command": command}
        return {"spec": {"template": {"spec": {"containers": [container]}}}}

    def mark(self, _ns, _pod, _container, marker):
        self.log += marker + "\n"

    def after(self, _ns, _pod, _container, marker):
        _, found, after = self.log.partition(marker)
        return after if found else ""

    def signal(self, _ns, _pod, _container, name):
        self.signals.append((self.now, name))
        self.on_signal.get(name, lambda: None)()

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds
        for at in sorted(self.events):
            if at <= self.now:
                self.events.pop(at)()

    def restart_in_place(self, script):
        script.restart_in_place("x", "x-worker", "celery-worker", "main")

    def restart_now(self, script):
        args = argparse.Namespace(
            namespace="x",
            deployment="x-worker",
            container="celery-worker",
            selector="app=x",
            app_dir="/checkout",
            src_path="/src",
            app_row="x-app",
            beat_selector=None,
        )
        script.restart_now(args)


BANNER = " -------------- celery@x-worker v5.6.2 (recovery)\n"
RESTARTING = "Restarting celery worker (celery -A main worker)\n"


@pytest.fixture
def pod(script, monkeypatch):
    return FakePod(script, monkeypatch)


def test_a_task_holding_the_restart_gets_sigquit_after_the_grace_period(script, pod):
    def quit_():
        pod.pool = set()
        pod.log += "worker: Cold shutdown (MainProcess)\n" + BANNER

    pod.on_signal = {
        "SIGHUP": lambda: setattr(pod, "log", pod.log + RESTARTING),
        "SIGQUIT": quit_,
    }
    pod.restart_in_place(script)
    assert [name for _, name in pod.signals] == ["SIGHUP", "SIGQUIT"]
    assert pod.signals[1][0] >= 5


def test_a_drain_that_finishes_on_its_own_gets_no_sigquit(script, pod):
    """The old pool going away means the restart is under way; the banner may
    still take longer than the grace period (a slow import).
    """
    pod.on_signal = {"SIGHUP": lambda: setattr(pod, "log", pod.log + RESTARTING)}
    pod.events = {
        3: lambda: setattr(pod, "pool", set()),
        9: lambda: setattr(pod, "log", pod.log + BANNER),
    }
    pod.restart_in_place(script)
    assert [name for _, name in pod.signals] == ["SIGHUP"]


def test_a_container_restart_during_the_wait_is_reported_as_a_crash(script, pod):
    def crash():
        pod.restarts = 1
        pod.log = BANNER  # a fresh container's log: no marker, no restart line

    pod.on_signal = {"SIGHUP": lambda: setattr(pod, "log", pod.log + RESTARTING)}
    pod.events = {2: crash}
    with pytest.raises(SystemExit, match="Celery exited while loading your checkout"):
        pod.restart_in_place(script)
    assert [name for _, name in pod.signals] == ["SIGHUP"]


def test_a_container_held_in_restart_backoff_is_reported_as_a_crash(script, pod):
    """After repeated crashes the kubelet waits before starting the container
    again, and its restart count only grows once it does.
    """
    pod.on_signal = {"SIGHUP": lambda: setattr(pod, "log", pod.log + RESTARTING)}
    pod.events = {2: lambda: setattr(pod, "running", False)}
    with pytest.raises(SystemExit, match="Celery exited while loading your checkout"):
        pod.restart_in_place(script)


def test_a_click_during_a_stack_recreate_waits_for_the_deployment(script, pod):
    pod.deployment_exists = False
    pod.on_signal = {
        "SIGHUP": lambda: setattr(pod, "log", pod.log + RESTARTING + BANNER)
    }
    pod.restart_now(script)
    assert pod.copies


def test_a_rollout_waits_for_the_app_resource_first(script, monkeypatch):
    calls = []
    monkeypatch.setattr(script, "log", lambda _message: None)
    monkeypatch.setattr(
        script, "wait_for_app_row", lambda row: calls.append(("wait", row))
    )

    def kubectl(_ns, *args, **_kwargs):
        calls.append(args[:2])
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(script, "kubectl", kubectl)
    script.main(["rollout", "x", "x-beat", "--app-row", "x-app"])
    assert calls[:2] == [("wait", "x-app"), ("rollout", "restart")]


def test_nothing_is_copied_or_signalled_until_the_worker_answers_a_ping(script, pod):
    """A signal before Celery has its handlers is dropped, so the restart waits
    for a pong first.
    """
    pod.answers_pings = False
    pod.events = {4: lambda: setattr(pod, "answers_pings", True)}
    pod.on_signal = {
        "SIGHUP": lambda: setattr(pod, "log", pod.log + RESTARTING + BANNER)
    }
    pod.restart_now(script)
    assert pod.copies
    assert pod.copies[0] >= 4
    assert pod.signals[0][0] >= pod.copies[0]
