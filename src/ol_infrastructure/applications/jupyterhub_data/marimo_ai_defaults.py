"""Merge platform AI defaults into a user's marimo config at server start.

Shipped into single-user pods and run from the postStart hook, not imported by
the Pulumi program. marimo reads a single user config file from the persistent
home volume and writes a full default one the first time it starts, so seeding
a file with ``cp -n`` would never reach existing users. This fills in keys a
user has not set, and never overrides a value the user chose.

The defaults last applied are recorded in a marker file next to the config.
That lets a change of platform default follow through to users who never
changed the seeded value, while leaving users who picked their own model alone:

- A value still equal to the previously seeded default is replaced with the new
  one. A user who explicitly picked that same model is indistinguishable, and
  moves with the default too.
- In a list, previously seeded entries the new defaults no longer contain are
  removed, and new ones are added. A default the user removed stays removed
  until the defaults change.

``tomlkit`` is a marimo dependency, so it is present wherever marimo is.
"""

import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

import tomlkit

MARKER_NAME = ".ol-ai-defaults-applied.json"


def user_config_path() -> Path:
    """Return the file marimo will read, mirroring its own lookup.

    The server's working directory is the home directory, so a ``.marimo.toml``
    there wins; otherwise marimo uses the XDG path.

    :returns: The path of the user's marimo config file.

    :rtype: Path
    """
    home_config = Path.home() / ".marimo.toml"
    if home_config.is_file():
        return home_config
    xdg_config_home = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return xdg_config_home / "marimo" / "marimo.toml"


def apply_defaults(
    target: Any, defaults: dict[str, Any], previous: dict[str, Any]
) -> None:
    """Apply ``defaults`` to ``target`` without overriding user choices.

    :param target: A tomlkit table (or document) to update in place.
    :type target: Any

    :param defaults: The defaults to apply now.
    :type defaults: dict[str, Any]

    :param previous: The defaults applied last time, at the same nesting level.
        Empty on first run.
    :type previous: dict[str, Any]
    """
    for key, value in defaults.items():
        seeded = previous.get(key)
        if isinstance(value, dict):
            if key not in target:
                target[key] = tomlkit.table()
            apply_defaults(target[key], value, seeded or {})
        elif key not in target:
            target[key] = value
        elif isinstance(value, list):
            current = target[key]
            for item in seeded or []:
                if item not in value and item in current:
                    current.remove(item)
            current.extend(item for item in value if item not in current)
        elif seeded is not None and target[key] == seeded:
            target[key] = value


def write_atomically(path: Path, content: str) -> None:
    """Replace ``path`` with ``content`` so a killed pod can't truncate it.

    :param path: The file to write.
    :type path: Path

    :param content: The new file contents.
    :type content: str
    """
    with tempfile.NamedTemporaryFile(
        "w", dir=path.parent, prefix=f".{path.name}.", delete=False
    ) as tmp:
        tmp.write(content)
    Path(tmp.name).replace(path)


def main(defaults_path: Path) -> None:
    """Apply the defaults at ``defaults_path`` to the user's marimo config.

    :param defaults_path: A JSON file of marimo config defaults.
    :type defaults_path: Path
    """
    defaults = json.loads(defaults_path.read_text())
    config_path = user_config_path()
    marker = config_path.parent / MARKER_NAME
    previous = json.loads(marker.read_text()) if marker.is_file() else {}
    if previous == defaults:
        return
    config_path.parent.mkdir(parents=True, exist_ok=True)
    document = (
        tomlkit.parse(config_path.read_text())
        if config_path.exists()
        else tomlkit.document()
    )
    apply_defaults(document, defaults, previous)
    write_atomically(config_path, tomlkit.dumps(document))
    write_atomically(marker, json.dumps(defaults))


if __name__ == "__main__":
    main(Path(sys.argv[1]))
