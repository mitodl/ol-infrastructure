"""The stale-lock script removes exactly the lock a dead run of ours left behind."""

import json
import os
import pwd
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).parents[2] / "local-dev/scripts/pulumi-clear-stale-lock.sh"


def _dead_pid() -> int:
    proc = subprocess.Popen(["true"])  # noqa: S607
    proc.wait()
    return proc.pid


def _lock(lock_dir: Path, name: str, pid: int, username: str) -> Path:
    path = lock_dir / f"{name}.json"
    path.write_text(json.dumps({"pid": pid, "username": username}))
    return path


def test_removes_only_our_lock_whose_process_is_gone(tmp_path):
    lock_dir = tmp_path / "locks"
    lock_dir.mkdir()
    me = pwd.getpwuid(os.getuid()).pw_name  # what `id -un` prints
    dead_mine = _lock(lock_dir, "dead-mine", _dead_pid(), me)
    live_mine = _lock(lock_dir, "live-mine", os.getpid(), me)
    dead_theirs = _lock(lock_dir, "dead-theirs", _dead_pid(), "someone-else")

    result = subprocess.run(  # noqa: S603
        [str(SCRIPT), str(lock_dir)], check=True, capture_output=True, text=True
    )

    assert not dead_mine.exists()
    assert live_mine.exists()
    assert dead_theirs.exists()
    assert lock_dir.is_dir()
    assert "dead-mine.json" in result.stderr


def test_a_missing_lock_dir_is_not_an_error(tmp_path):
    subprocess.run(  # noqa: S603
        [str(SCRIPT), str(tmp_path / "absent")], check=True, capture_output=True
    )
