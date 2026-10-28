"""pulumi-up-retry.sh with pulumi stubbed to fail a set number of times."""

import getpass
import json
import os
import signal
import stat
import subprocess
import time
from pathlib import Path

REPO = Path(__file__).parents[2]
SCRIPT = REPO / "local-dev/scripts/pulumi-up-retry.sh"
# Logs each call's arguments, then fails until it has been called $STUB_FAILS times.
STUB = """#!/usr/bin/env bash
echo "$*" >> "$STUB_LOG"
calls=$(wc -l < "$STUB_LOG")
[ "$calls" -gt "$STUB_FAILS" ] || { echo "stub failure $calls" >&2; exit 7; }
"""


def _run(tmp_path: Path, fails: int, attempts: int) -> tuple[int, list[str], str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "pulumi"
    stub.write_text(STUB)
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
    log = tmp_path / "calls.log"
    log.touch()
    result = subprocess.run(  # noqa: S603
        [str(SCRIPT), str(attempts), str(tmp_path / "locks"), "--yes", "--stack", "s"],
        env={
            **os.environ,
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "STUB_LOG": str(log),
            "STUB_FAILS": str(fails),
        },
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode, log.read_text().splitlines(), result.stderr


def test_success_runs_once(tmp_path: Path) -> None:
    code, calls, _ = _run(tmp_path, fails=0, attempts=2)
    assert code == 0
    assert calls == ["up --yes --stack s"]


def test_one_failure_is_retried(tmp_path: Path) -> None:
    code, calls, stderr = _run(tmp_path, fails=1, attempts=2)
    assert code == 0
    assert len(calls) == 2
    assert "stub failure 1" in stderr
    assert "attempt 1 of 2" in stderr


def test_persistent_failure_stops_at_the_limit(tmp_path: Path) -> None:
    code, calls, stderr = _run(tmp_path, fails=5, attempts=2)
    assert code == 7
    assert len(calls) == 2
    assert "attempt 2 of 2" in stderr


# Handles Ctrl-C like Pulumi does: logs, then exits 1 rather than dying of it.
INTERRUPTIBLE_STUB = """#!/usr/bin/env bash
trap 'exit 1' INT
echo "$*" >> "$STUB_LOG"
sleep 10
exit 7
"""


def test_ctrl_c_stops_the_wrapper_instead_of_retrying(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "pulumi"
    stub.write_text(INTERRUPTIBLE_STUB)
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
    log = tmp_path / "calls.log"
    log.touch()
    proc = subprocess.Popen(  # noqa: S603
        [str(SCRIPT), "2", str(tmp_path / "locks"), "--yes"],
        env={
            **os.environ,
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "STUB_LOG": str(log),
        },
        start_new_session=True,
    )
    deadline = time.monotonic() + 5
    while not log.read_text() and time.monotonic() < deadline:
        time.sleep(0.05)
    # A terminal's Ctrl-C reaches the whole foreground process group.
    os.killpg(proc.pid, signal.SIGINT)
    assert proc.wait(timeout=5) == 130
    assert len(log.read_text().splitlines()) == 1


# Fails while any lock file is left, as Pulumi does on a locked stack.
LOCK_CHECKING_STUB = """#!/usr/bin/env bash
echo "$*" >> "$STUB_LOG"
if compgen -G "$LOCK_DIR/*.json" >/dev/null; then
    echo "error: the stack is currently locked" >&2
    exit 255
fi
"""


def test_a_dead_runs_lock_is_cleared_before_the_attempt(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "pulumi"
    stub.write_text(LOCK_CHECKING_STUB)
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
    log = tmp_path / "calls.log"
    log.touch()
    lock_dir = tmp_path / "locks"
    lock_dir.mkdir()
    # A finished process: its PID is free by the time the wrapper looks.
    dead_pid = subprocess.Popen(["true"])  # noqa: S607
    dead_pid.wait()
    (lock_dir / "stale.json").write_text(
        json.dumps({"pid": dead_pid.pid, "username": getpass.getuser()})
    )
    result = subprocess.run(  # noqa: S603
        [str(SCRIPT), "2", str(lock_dir), "--yes"],
        env={
            **os.environ,
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "STUB_LOG": str(log),
            "LOCK_DIR": str(lock_dir),
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert len(log.read_text().splitlines()) == 1
    assert "removing stale pulumi lock stale.json" in result.stderr
