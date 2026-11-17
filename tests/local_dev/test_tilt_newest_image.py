"""tilt-newest-image.sh picks the image an apply deploys, with tilt stubbed out."""

import json
import os
import stat
import subprocess
from pathlib import Path
from typing import Any

SCRIPT = Path(__file__).parents[2] / "local-dev/scripts/tilt-newest-image.sh"
REPOSITORY = "k3d-registry.localhost:5000/mitodl_x-app"


def _completed(resource: str, image: str, tag: str, finished: str) -> dict[str, Any]:
    return {
        "metadata": {"name": f"{resource}:{image}"},
        "status": {
            "ref": f"localhost:5001/{image}:{tag}",
            "completed": {"finishedAt": finished},
        },
    }


def _run(tmp_path: Path, records: list[dict[str, Any]], handed: str | None) -> str:
    stub = tmp_path / "tilt"
    stub.write_text(
        f"#!/usr/bin/env bash\ncat <<'JSON'\n{json.dumps({'items': records})}\nJSON\n"
    )
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
    env = {**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}"}
    env.pop("TILT_IMAGE_0", None)
    if handed:
        env["TILT_IMAGE_0"] = handed
    result = subprocess.run(  # noqa: S603
        [str(SCRIPT)], capture_output=True, text=True, env=env, check=True
    )
    return result.stdout.strip()


def test_an_older_image_is_replaced_by_the_newest_build_of_the_same_image(tmp_path):
    records = [
        _completed("x-app", "mitodl_x-app", "tilt-new", "2026-11-17T12:45:00.000000Z"),
        _completed(
            "x-worker", "mitodl_x-app", "tilt-old", "2026-11-16T09:00:00.000000Z"
        ),
        _completed(
            "y-app", "mitodl_y-app", "tilt-other", "2026-11-17T15:00:00.000000Z"
        ),
    ]
    assert _run(tmp_path, records, f"{REPOSITORY}:tilt-old") == f"{REPOSITORY}:tilt-new"


def test_the_image_tilt_handed_over_stands_when_no_record_names_it(tmp_path):
    """The worker built tilt-new, then its next build failed: a failure has no ref."""
    records = [
        _completed("x-app", "mitodl_x-app", "tilt-old", "2026-11-16T09:00:00.000000Z"),
        {
            "metadata": {"name": "x-worker:mitodl_x-app"},
            "status": {
                "completed": {
                    "finishedAt": "2026-11-17T13:00:00.000000Z",
                    "error": "build failed",
                }
            },
        },
    ]
    assert _run(tmp_path, records, f"{REPOSITORY}:tilt-new") == f"{REPOSITORY}:tilt-new"


def test_the_image_tilt_handed_over_stands_while_a_build_is_still_recorded_as_running(
    tmp_path,
):
    """Beat's rebuild of reverted source is tilt-a again, not yet in its record.

    Tags follow the image's content, so the app's older record names the same
    tag, and the worker's tilt-b is the newest build the records hold.
    """
    records = [
        _completed("x-app", "mitodl_x-app", "tilt-a", "2026-11-16T09:00:00.000000Z"),
        _completed("x-worker", "mitodl_x-app", "tilt-b", "2026-11-17T09:00:00.000000Z"),
        {
            "metadata": {"name": "x-beat:mitodl_x-app"},
            "status": {"building": {"startedAt": "2026-11-17T12:44:00.000000Z"}},
        },
    ]
    assert _run(tmp_path, records, f"{REPOSITORY}:tilt-a") == f"{REPOSITORY}:tilt-a"


def test_nothing_is_printed_when_tilt_built_no_image(tmp_path):
    assert _run(tmp_path, [], None) == ""
