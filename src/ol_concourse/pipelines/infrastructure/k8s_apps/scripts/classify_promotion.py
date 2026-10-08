"""Turn a Production preview into the release issue the gate waits on.

Runs in the QA job after it previews Production. One issue carries one
approval for everything the Production deploy would change: the app commits
(when a release is waiting) and the infrastructure diff (when there is one).

    new release? | Production diff | issue
    yes          | non-empty       | ``Release <app> <version>``: checklist + diff
    yes          | empty           | ``Release <app> <version>``: checklist only
    no           | non-empty       | ``Release <app> infrastructure @ <live>``: diff
    no           | empty           | none: ``skip`` is written and the put posts nothing

The title keeps the ``Release <app>`` prefix so the release gate, which
matches on it, still sees every row.

A release is new when the latest cut version is not the one Production is
running. What Production is running comes from the release resource's
``production_version`` and ``production_state``, and this refuses to decide
when the state is ``unknown``: guessing would either open an issue for a
release that is already live or hide one that is not. ``none`` (Production has
never had a successful deployment) is a first release.

``pipeline.py`` inlines this file's text into the task (``python3 -c``), so it
must stay standard-library only and self-contained.

Environment:
    APP_NAME: the app.
    VERSION_FILE: ``<release>/version``, the latest cut release.
    PRODUCTION_VERSION_FILE: ``production_version`` from a fresh release get.
    PRODUCTION_STATE_FILE: ``production_state`` from the same get.
    CHECKLIST_FILE: ``<release>/checklist.md`` from the release resource.
    PREVIEW_SUMMARY_FILE: the Production preview's markdown summary.
    NO_CHANGES_MARKER_FILE: written next to the summary when it found no changes.
    OUTPUT_DIR: directory to write ``title``, ``body.md`` and maybe ``skip`` into.
"""

import os
import sys
from pathlib import Path

NO_INFRA_CHANGES = "No infrastructure changes: Production already matches this code."


def is_new_release(version: str, production_version: str, state: str) -> bool:
    """Return whether *version* is waiting to be approved.

    Raises ValueError when that cannot be decided.
    """
    if not version:
        msg = "the release resource published no version"
        raise ValueError(msg)
    if state == "none":
        return True
    if state == "known" and production_version:
        return version != production_version
    msg = (
        "the release resource could not tell what Production is running "
        f"(state {state!r}, version {production_version!r})"
    )
    raise ValueError(msg)


def classify(
    app: str,
    version: str,
    *,
    new_release: bool,
    has_diff: bool,
) -> tuple[str, bool]:
    """Return ``(issue title, skip)``; the title is empty when skipping."""
    if new_release:
        return f"Release {app} {version}", False
    if has_diff:
        return f"Release {app} infrastructure @ {version}", False
    return "", True


def compose_body(
    checklist: str,
    summary: str,
    *,
    new_release: bool,
    has_diff: bool,
) -> str:
    """Compose the issue body, checklist first so its version header leads."""
    parts = []
    if new_release:
        parts.append(checklist.rstrip())
    parts.append(
        "## Infrastructure changes (Production preview)\n\n"
        + (summary.strip() if has_diff else NO_INFRA_CHANGES)
    )
    return "\n\n".join(parts) + "\n"


def _read(name: str) -> str:
    return Path(os.environ[name]).read_text().strip()


def main() -> None:
    """Decide the issue and write its title, body and skip marker."""
    app = os.environ["APP_NAME"]
    version = _read("VERSION_FILE")
    try:
        new_release = is_new_release(
            version,
            _read("PRODUCTION_VERSION_FILE"),
            _read("PRODUCTION_STATE_FILE"),
        )
    except (ValueError, OSError) as error:
        sys.exit(f"Refusing to classify {app}: {error}. No issue was posted.")

    summary_file = Path(os.environ["PREVIEW_SUMMARY_FILE"])
    if not summary_file.is_file():
        # No summary means the preview did not report; guessing "no changes"
        # here would let an unreviewed diff through or hide one.
        sys.exit(
            f"Refusing to classify {app}: the Production preview left no "
            f"summary at {summary_file}."
        )
    has_diff = not Path(os.environ["NO_CHANGES_MARKER_FILE"]).exists()

    title, skip = classify(app, version, new_release=new_release, has_diff=has_diff)
    output = Path(os.environ["OUTPUT_DIR"])
    output.mkdir(parents=True, exist_ok=True)
    # load_var needs a title file to read even when nothing is posted.
    (output / "title").write_text(title or f"Release {app} infrastructure @ {version}")
    if skip:
        (output / "skip").write_text("nothing to approve\n")
        print(f"{app}: new release no, infrastructure diff no. Nothing to approve.")  # noqa: T201
        return

    checklist = Path(os.environ["CHECKLIST_FILE"]).read_text() if new_release else ""
    body = compose_body(
        checklist,
        summary_file.read_text(),
        new_release=new_release,
        has_diff=has_diff,
    )
    (output / "body.md").write_text(body)
    print(  # noqa: T201
        f"{app}: new release {'yes' if new_release else 'no'}, "
        f"infrastructure diff {'yes' if has_diff else 'no'}. Issue: {title!r}."
    )


if __name__ == "__main__":
    main()
