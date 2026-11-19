"""wait-for-live-update.sh holds a command until a marker file exists.

Run as a container runs it: the script's text handed to ``sh -c`` (tiltlib.star's
live_update_wait), then a name for ``$0``, the marker, the limit and the command.
"""

import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[2] / "local-dev/scripts/wait-for-live-update.sh"


def _start(marker: Path, seconds: int) -> subprocess.Popen[str]:
    return subprocess.Popen(  # noqa: S603
        [  # noqa: S607
            "sh",
            "-c",
            SCRIPT.read_text(),
            "wait-for-live-update",
            str(marker),
            str(seconds),
            "echo",
            "started",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def test_the_command_starts_once_the_marker_exists(tmp_path):
    marker = tmp_path / "synced"
    process = _start(marker, seconds=30)
    with pytest.raises(subprocess.TimeoutExpired):
        process.wait(timeout=1)
    marker.touch()
    stdout, stderr = process.communicate(timeout=5)
    assert (stdout, stderr) == ("started\n", "")


def test_a_stop_during_the_wait_ends_it_without_starting_the_command(tmp_path):
    """A container's first process is sent SIGTERM only if it handles it."""
    process = _start(tmp_path / "synced", seconds=30)
    with pytest.raises(subprocess.TimeoutExpired):
        process.wait(timeout=1)
    process.terminate()
    stdout, _ = process.communicate(timeout=5)
    assert (stdout, process.returncode) == ("", 143)


def test_the_command_starts_without_the_marker_once_the_limit_passes(tmp_path):
    process = _start(tmp_path / "synced", seconds=1)
    stdout, stderr = process.communicate(timeout=5)
    assert stdout == "started\n"
    assert "No copy from Tilt after 1 s" in stderr
