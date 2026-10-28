"""pulumi-up-retry.sh with pulumi stubbed to fail a set number of times."""

import os
import stat
import subprocess
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
        [str(SCRIPT), str(attempts), "--yes", "--stack", "s"],
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
