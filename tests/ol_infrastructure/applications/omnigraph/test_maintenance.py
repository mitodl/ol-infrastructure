"""Tests for the scheduled omnigraph store-maintenance sweep.

The sweep is a generated shell script, and every way it can be wrong is quiet.
A missing ``--yes`` makes every cleanup run refuse and delete nothing; a loop
that stops at the first bad graph skips the rest of the list without saying so;
a swallowed exit code reports a Job as successful having maintained nothing.
None of those show up as an error anybody sees, so they are pinned here.

The script is executed for real against ``/bin/sh`` with a stub ``omnigraph`` on
PATH, rather than asserted against as a string: the properties that matter are
behavioural (does it keep going, does it exit non-zero, what arguments does the
CLI actually receive) and a substring assertion would pass on a script that
does not run.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

from ol_infrastructure.applications.omnigraph.maintenance import (
    DEFAULT_CLEANUP_OLDER_THAN,
    _sweep_script,
)

GRAPHS = ["council", "code-bridge", "code-github-com-mitodl-ol-django"]

CLEANUP_ARGS = ["--older-than", DEFAULT_CLEANUP_OLDER_THAN, "--confirm", "--yes"]

# Deliberately NOT the real bucket or actor. Both reach the script through the
# environment, and what is under test is that it passes them through unaltered —
# nothing here pins a naming convention. The real values are built in
# data_tier.py (`ol-data-witan-<env>` from the OLBucket, and CLUSTER_APPLY_ACTOR),
# so a realistic-looking literal here would only send someone renaming the bucket
# to a test that never needed to change.
STORAGE_ROOT = "s3://test-storage-root"
MAINTENANCE_ACTOR = "test-actor"


class SweepResult:
    """A sweep run: its exit status and the argv of each ``omnigraph`` call."""

    def __init__(self, returncode: int, calls: list[list[str]], stdout: str):
        self.returncode = returncode
        self.calls = calls
        self.stdout = stdout

    def graphs_swept(self) -> list[str]:
        """Return the ``--graph`` value of every call, in call order."""
        return [call[call.index("--graph") + 1] for call in self.calls]


@pytest.fixture
def run_sweep(tmp_path: Path):
    """Run a generated sweep against a stub ``omnigraph`` that records argv.

    ``fail_for`` names graph ids the stub should exit non-zero for, which is how
    an unopenable graph presents — the real CLI errors with "graph `X` is not
    applied in cluster ...".
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls_file = tmp_path / "calls.txt"

    def _run(script: str, fail_for: tuple[str, ...] = ()) -> SweepResult:
        # Records one line of tab-separated argv per invocation, then fails for
        # the named graphs. Written per-run because the failure set is part of
        # the stub.
        fail_cases = "\n".join(f"    {graph}) exit 1 ;;" for graph in fail_for)
        (bin_dir / "omnigraph").write_text(
            "#!/bin/sh\n"
            f'printf "%s\\t" "$@" >> "{calls_file}"\n'
            f'printf "\\n" >> "{calls_file}"\n'
            'graph=""\n'
            "while [ $# -gt 0 ]; do\n"
            '    if [ "$1" = "--graph" ]; then graph="$2"; fi\n'
            "    shift\n"
            "done\n"
            'case "${graph}" in\n'
            f"{fail_cases}\n"
            "esac\n"
            "exit 0\n"
        )
        (bin_dir / "omnigraph").chmod(0o755)
        calls_file.write_text("")

        completed = subprocess.run(  # noqa: S603
            [shutil.which("sh") or "/bin/sh", "-c", script],
            capture_output=True,
            text=True,
            env={
                "PATH": str(bin_dir),
                "OMNIGRAPH_STORAGE_ROOT": STORAGE_ROOT,
                "OMNIGRAPH_MAINTENANCE_ACTOR": MAINTENANCE_ACTOR,
            },
            check=False,
        )
        calls = [
            line.rstrip("\t").split("\t")
            for line in calls_file.read_text().splitlines()
            if line.strip()
        ]
        return SweepResult(completed.returncode, calls, completed.stdout)

    return _run


def test_optimize_sweeps_every_declared_graph(run_sweep):
    """Every graph in cluster.yaml gets maintained, not just the first."""
    result = run_sweep(_sweep_script("optimize", [], GRAPHS))

    assert result.returncode == 0
    assert result.graphs_swept() == GRAPHS


def test_sweep_addresses_the_cluster_root_not_a_bare_uri(run_sweep):
    """``--cluster <root> --graph <id>`` is the only addressing that works.

    A bare positional storage URI addresses a *single graph store* and errors
    against a cluster root, and omitting ``--graph`` is a hard error listing the
    available graphs. Both are easy to reintroduce and neither fails until it
    runs in the cluster.
    """
    result = run_sweep(_sweep_script("optimize", [], ["council"]))

    (call,) = result.calls
    assert call == [
        "optimize",
        "--cluster",
        STORAGE_ROOT,
        "--graph",
        "council",
        "--as",
        MAINTENANCE_ACTOR,
    ]


def test_cleanup_passes_confirm_and_yes(run_sweep):
    """Both flags are required, for different reasons, on an s3:// store.

    ``--confirm`` arms the destructive run at all; ``--yes`` skips the
    confirmation an s3:// (non-local) scope otherwise demands, which a pod has
    no TTY to answer. Dropping either turns every scheduled run into a no-op.
    """
    result = run_sweep(_sweep_script("cleanup", CLEANUP_ARGS, ["council"]))

    (call,) = result.calls
    assert "--confirm" in call
    assert "--yes" in call


def test_cleanup_retention_is_an_age_not_a_version_count(run_sweep):
    """``--older-than`` alone — see maintenance.py for why ``--keep`` is out."""
    result = run_sweep(_sweep_script("cleanup", CLEANUP_ARGS, ["council"]))

    (call,) = result.calls
    assert call[call.index("--older-than") + 1] == DEFAULT_CLEANUP_OLDER_THAN
    assert "--keep" not in call


def test_a_misconfigured_retention_stays_one_argument(run_sweep):
    """``cleanup_older_than`` is operator-set Pulumi config, so it can be junk.

    Unquoted, the plausible typo ``30 days`` splits into two shell words and the
    CLI gets a stray positional argument — a confusing failure at 4am on a
    Sunday. Quoted, it arrives as one argument and comes back as a duration
    parse error naming the value.
    """
    result = run_sweep(
        _sweep_script(
            "cleanup", ["--older-than", "30 days", "--confirm", "--yes"], ["council"]
        )
    )

    (call,) = result.calls
    assert call[call.index("--older-than") + 1] == "30 days"


def test_shell_metacharacters_in_config_do_not_escape_the_argument(run_sweep):
    """Nothing an operator can put in config should change what runs.

    Config is only settable by someone who could edit this program anyway, so
    this is not a privilege boundary — but a value that silently rewrites the
    command line is a failure mode worth foreclosing rather than reasoning
    about.
    """
    hostile = '30d"; echo owned; #'
    result = run_sweep(
        _sweep_script(
            "cleanup", ["--older-than", hostile, "--confirm", "--yes"], ["council"]
        )
    )

    # The whole argv, not just the one field: had the metacharacters escaped,
    # the `#` would have commented out everything after it and the flags below
    # would be missing from the recorded call.
    (call,) = result.calls
    assert call == [
        "cleanup",
        "--cluster",
        STORAGE_ROOT,
        "--graph",
        "council",
        "--as",
        MAINTENANCE_ACTOR,
        "--older-than",
        hostile,
        "--confirm",
        "--yes",
    ]
    assert "owned" not in result.stdout


def test_one_bad_graph_does_not_skip_the_rest(run_sweep):
    """A quarantined code graph must not cost every graph after it in the list.

    This is the whole reason the loop does not `set -e`: with one graph per
    managed repo, an early failure would silently drop the tail of the sweep.
    """
    result = run_sweep(_sweep_script("optimize", [], GRAPHS), fail_for=("code-bridge",))

    assert result.graphs_swept() == GRAPHS


def test_a_failed_graph_still_fails_the_job(run_sweep):
    """Continuing past a failure must not swallow it.

    A zero exit here reports a successful CronJob run that maintained less than
    it was asked to, which is the failure nobody would ever go looking for.
    """
    result = run_sweep(_sweep_script("optimize", [], GRAPHS), fail_for=("code-bridge",))

    assert result.returncode == 1


def test_a_fully_successful_sweep_says_so(run_sweep):
    """The success line is what distinguishes "swept" from "swept nothing"."""
    result = run_sweep(_sweep_script("optimize", [], GRAPHS))

    assert "all graphs completed" in result.stdout


def test_every_failed_graph_is_named(run_sweep):
    """An operator needs the whole failing set, not just the first one."""
    result = run_sweep(
        _sweep_script("optimize", [], GRAPHS),
        fail_for=("council", "code-github-com-mitodl-ol-django"),
    )

    assert result.returncode == 1
    assert result.graphs_swept() == GRAPHS
