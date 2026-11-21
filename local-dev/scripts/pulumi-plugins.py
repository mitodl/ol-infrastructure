#!/usr/bin/env python3
"""Install the provider plugins a local-dev Pulumi program uses, and no others.

    uv run --project <repo> python pulumi-plugins.py <stack> <package>...

Run from the program's directory, in the virtualenv Pulumi runs the program
in, before Pulumi commands that have PULUMI_DISABLE_AUTOMATIC_PLUGIN_ACQUISITION
set. It writes nothing to stdout (pulumi-app-apply.sh's stdout is Tilt's YAML).

Pulumi's Python host reports every pulumi-* package in the virtualenv as a
plugin the program needs, and Pulumi downloads any that are missing before the
program runs. This repository's virtualenv holds the providers of every
deployed stack (AWS, Azure Native and GCP alone are about 1.7 GB of binaries),
so each provider Renovate bumped on main cost the next Tilt start a download
the local-dev programs never load: tens of minutes on a slow link.

With automatic acquisition off, Pulumi loads only plugins already installed.
This installs the plugins of the named packages at the versions the virtualenv
pins, which the program registers, and every provider version the stack's
state records, which a refresh loads before the program runs. A plugin already
installed costs a fraction of a second.

A program that starts using another provider fails with "no resource plugin
... found in the workspace"; add that provider's package to the caller's list.
"""

import json
import subprocess
import sys
from importlib import resources

Plugin = tuple[str, str, str | None]


def pinned_plugin(package: str) -> Plugin:
    """Return the plugin the installed package registers its resources with."""
    meta = json.loads(
        resources.files(package).joinpath("pulumi-plugin.json").read_text()
    )
    return meta["name"], meta["version"].lstrip("v"), meta.get("server")


def state_plugins(stack: str) -> set[Plugin]:
    """Return the provider plugins the stack's last checkpoint records."""
    export = subprocess.run(  # noqa: S603
        ["pulumi", "stack", "export", "--stack", stack],  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
    )
    if export.returncode != 0:
        # A stack with no state yet has nothing to refresh.
        return set()
    deployment = json.loads(export.stdout).get("deployment") or {}
    plugins = set()
    for resource in deployment.get("resources") or []:
        name = resource["type"].removeprefix("pulumi:providers:")
        inputs = resource.get("inputs") or {}
        # `pulumi` is the built-in provider (stack references); it has no plugin.
        if name == resource["type"] or name == "pulumi" or not inputs.get("version"):
            continue
        plugins.add(
            (name, inputs["version"].lstrip("v"), inputs.get("pluginDownloadURL"))
        )
    return plugins


def main() -> None:
    """Install the plugins of the named packages and of the stack's state."""
    if len(sys.argv) < 3:  # noqa: PLR2004
        sys.exit(f"usage: {sys.argv[0]} <stack> <package>...")
    stack, packages = sys.argv[1], sys.argv[2:]
    plugins = {pinned_plugin(package) for package in packages} | state_plugins(stack)
    for name, version, server in sorted(plugins, key=lambda p: (p[0], p[1])):
        command = ["pulumi", "plugin", "install", "resource", name, version]
        if server:
            command += ["--server", server]
        subprocess.run(command, check=True, stdout=sys.stderr)  # noqa: S603


if __name__ == "__main__":
    main()
