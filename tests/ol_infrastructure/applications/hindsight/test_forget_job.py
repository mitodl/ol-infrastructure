"""The forget CronJob's settings that must agree with the script it runs."""

import ast

from ol_infrastructure.applications.hindsight import forget_job


def _script_constant(name: str) -> object:
    # The script's dependencies (ol-llm, psycopg) are in its own lockfile, not
    # this project's environment, so it is read rather than imported.
    tree = ast.parse((forget_job.SCRIPT_DIR / forget_job.SCRIPT_FILENAME).read_text())
    for node in tree.body:
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id == name
        ):
            return ast.literal_eval(node.value)
    msg = f"{forget_job.SCRIPT_FILENAME} assigns no {name}"
    raise AssertionError(msg)


def test_the_fail_job_rule_matches_the_scripts_cap_status() -> None:
    assert _script_constant("CAP_EXCEEDED_STATUS") == forget_job.CAP_EXCEEDED_STATUS


def test_the_lockfile_is_mounted_beside_the_script() -> None:
    assert f"{forget_job.SCRIPT_FILENAME}.lock" == forget_job.LOCK_FILENAME
    assert (forget_job.SCRIPT_DIR / forget_job.LOCK_FILENAME).is_file()
