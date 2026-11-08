#!/usr/bin/env python3
"""Tilt's apply command for one workload resource of a Pulumi-rendered app.

local-dev/tiltlib.star's pulumi_app_rows gives each Celery worker, Celery beat
and other Deployment of such an app its own Tilt resource, and this script is
what its update button runs (and what runs once at `tilt up`):

  workload-restart.py now <app> <deployment> --selector <labels> --container <name>
      --app-dir <checkout> --app-row <resource> [--namespace <ns>] [--src-path /src]
      [--beat-selector <labels> --beat-row <resource>]
      Restart a Celery worker on the checkout, now: copy the checkout into the
      running container, then have Celery restart itself in place (SIGHUP, and
      SIGQUIT if a task is still running a few seconds later). Reports the
      beat schedule as stale when the checkout's differs from the beat pod's.

  workload-restart.py rollout <app> <deployment> --app-row <resource>
      [--namespace <ns>]
      `kubectl rollout restart`: a new pod on the image's code.

Both first wait while the app resource is applying, and both end by printing
the Deployment's YAML, which Tilt reads as the resource's objects. Everything
else goes to stderr, where the resource's log shows it.
Only the standard library: Tilt runs this with the developer's python3.
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import secrets
import shlex
import subprocess
import sys
import tarfile
import time
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import IO, Any, NoReturn, cast

CONTEXT = "local-dev"
# Printed by Celery's SIGHUP handler (a print, not a log record, so no logging
# config hides it).
RESTART_MARKER = "Restarting celery worker"
# Printed by each worker process once its app has loaded.
BANNER_MARKER = "-------------- celery@"
# app.conf.beat_schedule is what beat reads, so a schedule kept in Django
# settings (CELERY_BEAT_SCHEDULE) and one set on the app compare alike.
# find_app accepts the worker command's -A value. A schedule object is compared
# by its __reduce__ (its class and constructor arguments), since its str rounds
# the interval ("1.00 hour" for 3600 s and 3610 s) and leaves out arguments
# such as mitxonline's OffsettingSchedule offset.
SCHEDULE_DUMP = """
import re, sys
from celery.app.utils import find_app
app = find_app(sys.argv[1])
for name, entry in sorted(app.conf.beat_schedule.items()):
    schedule = entry.get("schedule")
    if hasattr(schedule, "is_due"):
        schedule = schedule.__reduce__()
    line = " ".join(repr(v) for v in (name, entry.get("task"), schedule,
                    entry.get("args"), sorted((entry.get("kwargs") or {}).items()),
                    sorted((entry.get("options") or {}).items())))
    # A schedule class without a __repr__ prints its address, which differs per
    # process and would make every comparison a mismatch.
    print(re.sub(r" at 0x[0-9a-f]+", "", line))
"""
# The stat line's second field is the command in parentheses, which may hold
# spaces, so the fields are read after its closing parenthesis: ppid is the
# second of those.
CHILDREN_OF_PID1 = """
import os
for pid in os.listdir("/proc"):
    if pid.isdigit():
        try:
            stat = open(f"/proc/{pid}/stat").read()
        except OSError:
            continue
        if stat.rsplit(")", 1)[1].split()[1] == "1":
            print(pid)
"""


def log(message: str) -> None:
    sys.stderr.write(f"[workload-restart] {message}\n")
    sys.stderr.flush()


def kubectl(
    namespace: str, *args: str, stdin: str | None = None, check: bool = True
) -> subprocess.CompletedProcess[str]:
    cmd = ["kubectl", "--context", CONTEXT, "-n", namespace, *args]
    result = subprocess.run(  # noqa: S603
        cmd, input=stdin, capture_output=True, text=True, check=False
    )
    if check and result.returncode != 0:
        sys.exit(f"{shlex.join(cmd)} failed:\n{result.stderr.strip()}")
    return result


def exec_in(
    namespace: str, pod: str, container: str, *command: str, check: bool = True
) -> subprocess.CompletedProcess[str]:
    return kubectl(namespace, "exec", pod, "-c", container, "--", *command, check=check)


def tilt(*args: str, stdin: str | None = None) -> str | None:
    """Run the tilt CLI against the Tilt that runs this script (it passes
    TILT_PORT and TILT_HOST); return its stdout, or None when it fails, since
    nothing here is worth failing the restart over.
    """
    try:
        result = subprocess.run(  # noqa: S603
            ["tilt", *args],  # noqa: S607
            input=stdin,
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        log("no tilt on PATH")
        return None
    if result.returncode != 0:
        log(f"tilt {args[0]} failed: {result.stderr.strip()}")
        return None
    return result.stdout


def wait_for(
    what: str, check: Callable[[], Any], timeout: float, interval: float = 1.0
) -> Any:
    """Poll check until it returns a truthy value; exit the resource red otherwise."""
    deadline = time.monotonic() + timeout
    while True:
        result = check()
        if result:
            return result
        if time.monotonic() > deadline:
            sys.exit(f"gave up after {timeout:.0f}s {what}")
        time.sleep(interval)


def get_json(namespace: str, *args: str) -> dict[str, Any]:
    return json.loads(kubectl(namespace, "get", *args, "-o", "json").stdout)


def running_pod(namespace: str, selector: str, container: str) -> dict[str, Any] | None:
    """Return the newest pod matching selector whose container is running and
    that is not being deleted, as {"name", "started"}; None if none.
    """
    live = [
        {
            "name": pod["metadata"]["name"],
            "started": status["state"]["running"]["startedAt"],
        }
        for pod in get_json(namespace, "pods", "-l", selector)["items"]
        if not pod["metadata"].get("deletionTimestamp")
        for status in pod.get("status", {}).get("containerStatuses", [])
        if status["name"] == container and "running" in status.get("state", {})
    ]
    if not live:
        return None
    if len(live) > 1:
        log(f"{len(live)} running pods match {selector}; only the newest is restarted")
    return max(live, key=lambda p: p["started"])


def container_status(namespace: str, pod: str, container: str) -> dict[str, Any] | None:
    """Return the container's restart count, whether it is running, and the
    reason its last run ended, as {"restarts", "running", "last_reason"}; None
    once the pod is gone.
    """
    result = kubectl(namespace, "get", "pod", pod, "-o", "json", check=False)
    if result.returncode != 0:
        return None
    for status in json.loads(result.stdout)["status"].get("containerStatuses", []):
        if status["name"] == container:
            state = status.get("state", {})
            terminated = (
                state.get("terminated")
                or status.get("lastState", {}).get("terminated")
                or {}
            )
            return {
                "restarts": status["restartCount"],
                "running": "running" in state,
                "last_reason": terminated.get("reason"),
            }
    return None


def celery_app_from_command(command: Iterable[str]) -> str | None:
    """Return the -A value of a celery command line."""
    command = list(command)
    for i, arg in enumerate(command):
        if arg == "-A" and i + 1 < len(command):
            return command[i + 1]
    return None


def checkout_files(app_dir: Path) -> list[str]:
    """Paths to copy, relative to app_dir: tracked and untracked files that are
    not ignored, minus .claude/ (agent worktrees) and files deleted from the
    working tree but not yet from the index.
    """

    def git_z(*args: str) -> set[str]:
        out = subprocess.run(  # noqa: S603
            ["git", "-C", str(app_dir), "ls-files", "-z", *args],  # noqa: S607
            capture_output=True,
            check=True,
        ).stdout
        return {os.fsdecode(p) for p in out.split(b"\0") if p}

    present = git_z("-co", "--exclude-standard", "--", ":!.claude")
    deleted = git_z("-d")
    return sorted(present - deleted)


def copy_checkout(
    namespace: str, pod: str, container: str, app_dir: Path, src_path: str
) -> None:
    """Unpack the checkout at src_path in the container. Python builds the tar,
    streamed into the pod's `tar -x`, so macOS and Linux hosts send the same
    bytes (bsdtar adds xattrs and AppleDouble entries), without ownership, so
    the files land as the container's user.
    """
    files = checkout_files(app_dir)
    log(f"copying {len(files)} files from {app_dir} to {pod}:{src_path}")

    def anonymous(info: tarfile.TarInfo) -> tarfile.TarInfo:
        info.uid = info.gid = 0
        info.uname = info.gname = ""
        return info

    proc = subprocess.Popen(  # noqa: S603
        [  # noqa: S607
            "kubectl", "--context", CONTEXT, "-n", namespace,
            "exec", "-i", pod, "-c", container, "--", "tar", "-x", "-C", src_path,
        ],
        stdin=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )  # fmt: skip
    with tarfile.open(fileobj=cast("IO[bytes]", proc.stdin), mode="w|") as tar:
        for rel in files:
            tar.add(app_dir / rel, arcname=rel, recursive=False, filter=anonymous)
    _, err = proc.communicate()
    if proc.returncode != 0:
        sys.exit(f"copying the checkout into {pod} failed:\n{err.decode()}")


def signal_pid1(namespace: str, pod: str, container: str, signal_name: str) -> None:
    # os.kill rather than a kill binary, which not every image has.
    exec_in(
        namespace,
        pod,
        container,
        "python",
        "-c",
        f"import os, signal; os.kill(1, signal.{signal_name})",
    )


def pool_children(namespace: str, pod: str, container: str) -> set[str]:
    """Return the PIDs of PID 1's children (the prefork pool) in the container."""
    result = exec_in(
        namespace, pod, container, "python", "-c", CHILDREN_OF_PID1, check=False
    )
    return set(result.stdout.split())


def restarted(logs: str) -> bool:
    """Report whether logs show Celery's restart message followed by a new
    process's banner.
    """
    marker = logs.find(RESTART_MARKER)
    return marker >= 0 and BANNER_MARKER in logs[marker:]


def mark_log(namespace: str, pod: str, container: str, marker: str) -> None:
    """Write marker into the container's log through PID 1's stdout, so the
    lines a restart writes can be told from everything logged before it
    without comparing the host's clock with the node's.
    """
    exec_in(namespace, pod, container, "sh", "-c", f"echo '{marker}' > /proc/1/fd/1")


def log_after(namespace: str, pod: str, container: str, marker: str) -> str | None:
    """Return the container's log after marker: "" while the marker is not in
    it (a restarted container starts a new log), None if the log cannot be read
    (the pod is gone). --since is relative to the node's clock, so it holds
    however much the worker logs.
    """
    result = kubectl(
        namespace, "logs", pod, "-c", container, "--since=10m", check=False
    )
    if result.returncode != 0:
        return None
    _, found, after = result.stdout.partition(marker)
    return after if found else ""


def ping(namespace: str, pod: str, container: str, celery_app: str) -> bool:
    """Report whether the worker in pod answers a ping."""
    result = exec_in(
        namespace, pod, container,
        "celery", "-A", celery_app, "inspect", "ping",
        "-d", f"celery@{pod}", "--timeout", "3",
        check=False,
    )  # fmt: skip
    return result.returncode == 0 and "pong" in result.stdout


def print_deployment(namespace: str, name: str) -> None:
    sys.stdout.write(kubectl(namespace, "get", "deploy", name, "-o", "yaml").stdout)
    sys.stdout.flush()


class Restart:
    """One in-place restart of the Celery worker in a pod: what the container
    looked like before the signal, and what tells a finished restart, a crash
    and a vanished pod apart afterwards.
    """

    def __init__(self, ns: str, pod: str, container: str) -> None:
        self.ns, self.pod, self.container = ns, pod, container
        before = container_status(ns, pod, container)
        self.restarts_before = before["restarts"] if before else None
        self.last_reason = None
        # Only log lines after this marker count, so an earlier click's restart
        # (or the boot banner) cannot pass for this one.
        self.marker = (
            f"[workload-restart] restarting celery@{pod} ({secrets.token_hex(4)})"
        )
        mark_log(ns, pod, container, self.marker)
        self.old_pool = pool_children(ns, pod, container)

    def exit_pod_gone(self) -> NoReturn:
        sys.exit(
            f"{self.pod} went away during the restart (did the stack re-apply?); "
            "click again once the new pod is up"
        )

    def exit_crashed(self) -> NoReturn:
        if self.last_reason == "OOMKilled":
            sys.exit(
                "the container went over its memory limit and was killed during "
                "the restart, so it is back on the image's code; click again"
            )
        sys.exit(
            "Celery exited while loading your checkout; the traceback is above in "
            "this log. The container restarted and runs the image's code until the "
            "next click. Fix the error and click again."
        )

    def has_restarted(self) -> bool:
        tail = log_after(self.ns, self.pod, self.container, self.marker)
        if tail is None:
            self.exit_pod_gone()
        return restarted(tail)

    def crashed(self) -> bool:
        """Report whether the container stopped or restarted: a crash on import
        ends PID 1, and the container then starts a fresh log that never shows
        the restart. After repeated crashes the kubelet waits before starting
        it again, and the restart count grows only once it does.
        """
        status = container_status(self.ns, self.pod, self.container)
        if status is None:
            self.exit_pod_gone()
        self.last_reason = status["last_reason"]
        return status["restarts"] != self.restarts_before or not status["running"]

    def task_still_running(self) -> bool:
        return bool(self.old_pool & pool_children(self.ns, self.pod, self.container))


def restart_in_place(ns: str, pod: str, container: str, celery_app: str) -> None:
    """Have the Celery worker in pod restart on the files now in its container,
    and return once the restarted worker answers a ping. Exits the resource red
    if the container restarts instead, which is Celery crashing on those files.
    """
    restart = Restart(ns, pod, container)
    log("sending SIGHUP: Celery finishes running tasks, then restarts in place")
    signal_pid1(ns, pod, container, "SIGHUP")

    # Warm shutdown waits for running tasks without a limit, so a pre-signal
    # pool process still alive after 5 s gets SIGQUIT (cold shutdown), and the
    # restart still follows. Once the old pool is gone, SIGQUIT would reach the
    # new process instead (dropped before it has handlers, a cold shutdown
    # after), so none is sent.
    grace = time.monotonic() + 5
    deadline = time.monotonic() + 60
    quit_sent = False
    while not restart.has_restarted():
        if restart.crashed():
            restart.exit_crashed()
        if time.monotonic() > deadline:
            sys.exit(
                "gave up after 60s waiting for Celery to restart; "
                "read the log above, then click again"
            )
        if not quit_sent and time.monotonic() > grace and restart.task_still_running():
            log("a task is still running; sending SIGQUIT so the restart happens now")
            signal_pid1(ns, pod, container, "SIGQUIT")
            quit_sent = True
        time.sleep(1)

    wait_for(
        f"waiting for the restarted celery@{pod} to answer a ping; "
        "read the log above, then click again",
        lambda: restart.crashed() or ping(ns, pod, container, celery_app),
        timeout=45,
        interval=2,
    )
    if restart.crashed():
        restart.exit_crashed()


def wait_for_app_row(app_row: str) -> None:
    """Wait while the app resource is applying. Its apply can replace the
    worker's pod, and a restart of the pod about to be replaced would be lost
    together with the pending flag that led to the click.
    """

    def status() -> str:
        return (
            tilt("get", "uiresource", app_row, "-o", "jsonpath={.status.updateStatus}")
            or "ok"
        ).strip()

    if status() in ("in_progress", "pending"):
        log(f"{app_row} is applying; waiting for it before restarting the worker")
        wait_for(
            f"waiting for {app_row} to finish applying; click again once it is green",
            lambda: status() not in ("in_progress", "pending"),
            timeout=240,
            interval=3,
        )


def wait_for_rollout(ns: str, deployment: str) -> None:
    """Wait until the Deployment's current rollout is complete, so the pod
    restarted below is the one that stays.
    """
    result = kubectl(
        ns, "rollout", "status", f"deploy/{deployment}", "--timeout=120s", check=False
    )
    if result.returncode != 0:
        sys.exit(
            f"{deployment} is still rolling out; click again once its pod is Running:\n"
            + result.stderr.strip()
        )


def restart_now(args: argparse.Namespace) -> None:
    ns, deployment, container = args.namespace, args.deployment, args.container
    # A stack recreate deletes the Deployment, so it is read after the apply.
    wait_for_app_row(args.app_row)
    spec = get_json(ns, "deploy", deployment)["spec"]["template"]["spec"]
    (celery_container,) = [c for c in spec["containers"] if c["name"] == container]
    celery_app = celery_app_from_command(celery_container.get("command") or [])
    if not celery_app:
        sys.exit(f"{deployment}'s {container} command has no -A <celery app>")

    wait_for_rollout(ns, deployment)
    pod = wait_for(
        f"waiting for a running {container} container of {deployment}; "
        f"see kubectl --context {CONTEXT} -n {ns} get pods",
        lambda: running_pod(ns, args.selector, container),
        timeout=60,
    )
    name = pod["name"]
    # A signal sent before Celery installs its handlers is dropped (PID 1 of a
    # PID namespace only receives signals it handles); a pong proves they are
    # in. A worker that never answers is crash-looping or hung at startup.
    wait_for(
        f"waiting for celery@{name} to answer a ping; its log above says why not",
        lambda: ping(ns, name, container, celery_app),
        timeout=45,
        interval=2,
    )

    copy_checkout(ns, name, container, Path(args.app_dir), args.src_path)
    restart_in_place(ns, name, container, celery_app)
    log(f"celery@{name} is running your checkout")

    if args.beat_selector:
        check_beat_schedule(args, name, celery_app)
    print_deployment(ns, deployment)


def schedule_dump(
    namespace: str, pod: str, container: str, celery_app: str
) -> str | None:
    result = exec_in(
        namespace,
        pod,
        container,
        "python",
        "-c",
        SCHEDULE_DUMP,
        celery_app,
        check=False,
    )
    if result.returncode != 0:
        log(f"could not read the beat schedule in {pod}:\n{result.stderr.strip()}")
        return None
    return result.stdout


def check_beat_schedule(
    args: argparse.Namespace, worker_pod: str, celery_app: str
) -> None:
    """Compare the schedule the checkout defines (read in the worker pod, which
    now runs it) with the one beat runs (its pod runs the image's code), and
    put a warning button on the beat resource while they differ.
    """
    ns = args.namespace
    beat = running_pod(ns, args.beat_selector, args.beat_container)
    if beat is None:
        log("no running beat pod; skipping the beat schedule check")
        return
    ours = schedule_dump(ns, worker_pod, args.container, celery_app)
    theirs = schedule_dump(ns, beat["name"], args.beat_container, celery_app)
    if ours is None or theirs is None:
        return
    stale = ours != theirs
    if stale:
        diff = difflib.unified_diff(
            theirs.splitlines(),
            ours.splitlines(),
            "beat (image)",
            "checkout",
            lineterm="",
            n=0,
        )
        log(
            "the beat schedule in your checkout differs from beat's; click "
            f'"Beat schedule is stale: recreate stack" on {args.beat_row} '
            "to recreate the stack:\n" + "\n".join(diff)
        )
    else:
        log("beat runs the schedule your checkout defines")
    sync_beat_warning(ns, args.beat_row, args.app_row, stale=stale)


def sync_beat_warning(ns: str, beat_row: str, app_row: str, *, stale: bool) -> None:
    """Add the warning button to beat_row, or remove it; its click triggers
    app_row.
    """
    button = f"{ns}-beat-schedule-stale"
    if not stale:
        tilt(
            "delete",
            f"uibutton/{button}",
            f"cmd/{button}-recreate",
            "--ignore-not-found",
        )
        return
    manifest = f"""
apiVersion: tilt.dev/v1alpha1
kind: UIButton
metadata:
  name: {button}
spec:
  location: {{componentType: Resource, componentID: {beat_row}}}
  text: "Beat schedule is stale: recreate stack"
  iconName: warning
---
apiVersion: tilt.dev/v1alpha1
kind: Cmd
metadata:
  name: {button}-recreate
spec:
  args:
    - sh
    - -c
    - tilt delete uibutton/{button} --ignore-not-found && tilt trigger {app_row}
  startOn: {{uiButtons: [{button}]}}
"""
    tilt("apply", "-f", "-", stdin=manifest)


def rollout(args: argparse.Namespace) -> None:
    ns, deployment = args.namespace, args.deployment
    wait_for_app_row(args.app_row)
    log(f"rolling {deployment}")
    kubectl(ns, "rollout", "restart", f"deploy/{deployment}")
    result = kubectl(
        ns, "rollout", "status", f"deploy/{deployment}", "--timeout=180s", check=False
    )
    sys.stderr.write(result.stdout + result.stderr)
    if result.returncode != 0:
        sys.exit(result.returncode)
    print_deployment(ns, deployment)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    modes = parser.add_subparsers(dest="mode", required=True)
    now = modes.add_parser(
        "now", help="restart a Celery worker on the checkout, in place"
    )
    roll = modes.add_parser("rollout", help="kubectl rollout restart")
    for mode in (now, roll):
        mode.add_argument(
            "app", help="the app's name (its ol.mit.edu/application label)"
        )
        mode.add_argument("deployment")
        mode.add_argument("--namespace", help="the app's namespace, when not its name")
        mode.add_argument(
            "--app-row",
            required=True,
            help="the app's resource, whose apply can replace the pods",
        )
    now.add_argument(
        "--selector", required=True, help="label selector for the worker's pods"
    )
    now.add_argument("--container", required=True, help="the Celery container's name")
    now.add_argument("--app-dir", required=True, help="the checkout to copy")
    now.add_argument(
        "--src-path", default="/src", help="where the image keeps the code"
    )
    now.add_argument(
        "--beat-selector",
        help="label selector for the beat pods; enables the schedule check",
    )
    now.add_argument("--beat-container", default="celery-beat")
    now.add_argument(
        "--beat-row",
        help="the beat Deployment's resource, which gets the warning button",
    )
    now.set_defaults(run=restart_now)
    roll.set_defaults(run=rollout)
    args = parser.parse_args(argv)
    args.namespace = args.namespace or args.app
    if args.mode == "now" and bool(args.beat_selector) != bool(args.beat_row):
        parser.error("--beat-selector and --beat-row go together")
    args.run(args)


if __name__ == "__main__":
    main()
