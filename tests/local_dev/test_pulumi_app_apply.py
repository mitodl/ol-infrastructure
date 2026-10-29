"""pulumi-app-apply.sh's recovery step, with pulumi and kubectl stubbed out.

The stubs log their arguments; the tests pin that a live stack lock stops the
run before any of them is called, and that a stale one is cleared and the
apply goes ahead.
"""

import getpass
import json
import os
import shutil
import stat
import subprocess
from pathlib import Path

REPO = Path(__file__).parents[2]
SCRIPTS = (
    "pulumi-app-apply.sh",
    "pulumi-clear-stale-lock.sh",
    "pulumi-pending-objects.py",
)
STUB = """#!/usr/bin/env bash
echo "$(basename "$0") $*" >> "$STUB_LOG"
case "$*" in
  *"stack export"*) echo '{"version": 3, "deployment": {"pending_operations": []}}' ;;
esac
"""


def _tree(tmp_path: Path) -> tuple[Path, Path, Path]:
    scripts = tmp_path / "local-dev/scripts"
    scripts.mkdir(parents=True)
    for name in SCRIPTS:
        shutil.copy(REPO / "local-dev/scripts" / name, scripts / name)
    program = tmp_path / "local-dev/apps/x/pulumi"
    program.mkdir(parents=True)
    (program / "Pulumi.yaml").write_text("name: proj\nruntime: python\n")
    lock_dir = tmp_path / "local-dev/infra/.pulumi/.pulumi/locks/organization/proj/Dev"
    lock_dir.mkdir(parents=True)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for tool in ("uv", "kubectl"):
        stub = bin_dir / tool
        stub.write_text(STUB)
        stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
    return scripts / "pulumi-app-apply.sh", lock_dir, bin_dir


def _lock(lock_dir: Path, pid: int) -> None:
    lock = {"pid": pid, "username": getpass.getuser(), "timestamp": "t"}
    (lock_dir / "lock.json").write_text(json.dumps(lock))


def _run(script: Path, bin_dir: Path, tmp_path: Path):
    log = tmp_path / "calls.log"
    log.write_text("")
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "STUB_LOG": str(log),
    }
    result = subprocess.run(  # noqa: S603
        [str(script), "x", "up"], capture_output=True, text=True, env=env, check=False
    )
    return result, log.read_text()


def test_a_live_lock_stops_the_run_before_it_touches_anything(tmp_path):
    """Another run's lock means its pending creates are live objects: no export,
    no kubectl delete, no pulumi at all.
    """
    script, lock_dir, bin_dir = _tree(tmp_path)
    _lock(lock_dir, os.getpid())
    result, calls = _run(script, bin_dir, tmp_path)
    assert result.returncode == 1
    assert "holds the proj/Dev lock" in result.stderr
    assert calls == ""


def test_a_stale_lock_is_cleared_and_the_apply_goes_ahead(tmp_path):
    script, lock_dir, bin_dir = _tree(tmp_path)
    gone = subprocess.Popen(["true"])  # noqa: S607
    gone.wait()
    _lock(lock_dir, gone.pid)
    result, calls = _run(script, bin_dir, tmp_path)
    assert result.returncode == 0, result.stderr
    assert not list(lock_dir.glob("*.json"))
    assert "pulumi up" in calls
