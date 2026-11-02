"""The local mitxonline program's own inputs: the steps appended to migrate."""

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).parents[2]
MODULE = REPO / "local-dev/apps/mitxonline/pulumi/inputs.py"


def _load():
    spec = importlib.util.spec_from_file_location("mitxonline_inputs", MODULE)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


inputs = _load()


def test_with_local_steps_refuses_a_definition_without_pre_deploy_commands():
    """The local steps run after migrate in its shell; with no command to append
    to they would be skipped silently, so the program must stop instead.
    """
    with pytest.raises(ValueError, match="pre_deploy_commands"):
        inputs.with_local_steps(None, analytics_api=False)


# Stands in for the image's python: every `manage.py <command>` succeeds, and
# `manage.py shell` compiles its stdin, as Django's shell does before exec, and
# appends it to $SHELL_LOG.
_STUB_PYTHON = f"""#!{sys.executable}
import os, sys
if sys.argv[1:3] == ["manage.py", "shell"]:
    source = sys.stdin.read()
    compile(source, "manage.py shell stdin", "exec")
    with open(os.environ["SHELL_LOG"], "a") as log:
        log.write(source + "\\0")
"""


@pytest.mark.parametrize("enabled", [True, False])
def test_the_composed_pre_deploy_command_runs_each_script_in_one_shell(
    tmp_path, enabled
):
    """The steps are only checked as a whole command: a heredoc that swallows
    the next step is valid bash, and each script compiles on its own.
    """
    ((_, command),) = inputs.with_local_steps(
        [("migrate", ["/bin/bash", "-c", "python manage.py migrate --noinput"])],
        analytics_api=enabled,
    )
    stub = tmp_path / "python"
    stub.write_text(_STUB_PYTHON)
    stub.chmod(0o755)
    log = tmp_path / "shell.log"

    result = subprocess.run(  # noqa: S603
        [*command[:-1], command[-1]],
        env={
            **os.environ,
            "PATH": f"{tmp_path}:{os.environ['PATH']}",
            "SHELL_LOG": str(log),
        },
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    (shell_input,) = log.read_text().split("\0")[:-1]
    assert "OPENEDX_OAUTH_APP_CLIENT_ID" in shell_input
    assert (inputs.ANALYTICS_API_CLIENT_ID in shell_input) is enabled


def test_the_analytics_app_presents_the_credentials_the_job_registers():
    """secrets.yaml is the analytics app's copy of the pair; a drift between the
    two fails every org-manager check with a 502.
    """
    secret = yaml.safe_load(
        (REPO / "local-dev/apps/ol-analytics-api/secrets.yaml").read_text()
    )

    assert secret["stringData"][
        "OL_ANALYTICS_API_B2B_DASHBOARD_MITXONLINE_CLIENT_ID"
    ] == (inputs.ANALYTICS_API_CLIENT_ID)
    assert secret["stringData"][
        "OL_ANALYTICS_API_B2B_DASHBOARD_MITXONLINE_CLIENT_SECRET"
    ] == (inputs.ANALYTICS_API_CLIENT_SECRET)
