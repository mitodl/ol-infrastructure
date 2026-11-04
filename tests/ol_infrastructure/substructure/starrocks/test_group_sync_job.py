"""The reload step must re-read groups.txt on every run, waiting only after a change.

A follower FE re-reads its own local copy when it replays the ALTER, so one whose
kubelet had not projected the new file yet stays behind until the next ALTER.
"""

import subprocess
from pathlib import Path

import pytest

from ol_infrastructure.substructure.starrocks import group_sync_job


def _run(tmp_path: Path, *, changed: bool) -> list[str]:
    """Run the reload command with mysql and sleep replaced by recorders."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True)
    log = tmp_path / "log"
    for tool in ("mysql", "sleep"):
        stub = bin_dir / tool
        stub.write_text(f'#!/bin/sh\necho "{tool} $*" >> {log}\n')
        stub.chmod(0o755)
    work = tmp_path / "work"
    work.mkdir()
    if changed:
        (work / "changed").touch()
    command = group_sync_job.reload_command(
        "keycloak_file_groups", "groups/groups.txt", 9030, ssl_enabled=True
    ).replace(group_sync_job.WORK_MOUNT_PATH, str(work))
    subprocess.run(  # noqa: S603
        ["sh", "-c", command],  # noqa: S607
        check=True,
        env={
            "PATH": f"{bin_dir}:/usr/bin:/bin",
            "STARROCKS_HOST": "fe",
            "STARROCKS_USER": "v-group",
        },
    )
    return log.read_text().splitlines()


@pytest.mark.parametrize("changed", [True, False])
def test_alter_runs_every_time(tmp_path: Path, *, changed: bool) -> None:
    """The ALTER runs whether or not this run changed the file."""
    calls = _run(tmp_path, changed=changed)

    assert calls[-1] == (
        "mysql -hfe -P9030 -uv-group --ssl-mode=REQUIRED -e ALTER GROUP PROVIDER "
        'keycloak_file_groups SET ("group_file_url" = "groups/groups.txt");'
    )


def test_waits_for_kubelet_only_after_a_change(tmp_path: Path) -> None:
    """An unchanged file needs no projection wait."""
    assert _run(tmp_path / "a", changed=True)[0] == (
        f"sleep {group_sync_job.KUBELET_PROJECTION_WAIT_SECONDS}"
    )
    assert not any(c.startswith("sleep") for c in _run(tmp_path / "b", changed=False))
