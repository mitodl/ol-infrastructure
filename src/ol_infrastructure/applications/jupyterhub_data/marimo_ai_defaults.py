"""Merge platform AI defaults into a user's marimo config at server start.

Shipped into single-user pods and run from the postStart hook, not imported by
the Pulumi program. marimo reads a single user config file from the persistent
home volume and writes a full default one the first time it starts, so seeding
a file with ``cp -n`` would never reach existing users. This fills in only the
keys a user has not set, so choices made in Settings > AI survive restarts.

``tomlkit`` is a marimo dependency, so it is present wherever marimo is.
"""

import json
import os
import sys
from pathlib import Path
from typing import Any

import tomlkit


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


def merge_missing(target: Any, defaults: dict[str, Any]) -> None:
    """Add every key in ``defaults`` that ``target`` lacks, recursively.

    Lists are unioned rather than replaced, so a default model is added to
    ``custom_models`` without dropping the user's own entries.

    :param target: A tomlkit table (or document) to update in place.
    :type target: Any

    :param defaults: Default values to fill in.
    :type defaults: dict[str, Any]
    """
    for key, value in defaults.items():
        if isinstance(value, dict):
            if key not in target:
                target[key] = tomlkit.table()
            merge_missing(target[key], value)
        elif key not in target:
            target[key] = value
        elif isinstance(value, list):
            target[key].extend(item for item in value if item not in target[key])


def main(defaults_path: Path) -> None:
    """Merge the defaults at ``defaults_path`` into the user's marimo config.

    :param defaults_path: A JSON file of marimo config defaults.
    :type defaults_path: Path
    """
    defaults = json.loads(defaults_path.read_text())
    config_path = user_config_path()
    config_path.parent.mkdir(parents=True, exist_ok=True)
    document = (
        tomlkit.parse(config_path.read_text())
        if config_path.exists()
        else tomlkit.document()
    )
    merge_missing(document, defaults)
    config_path.write_text(tomlkit.dumps(document))


if __name__ == "__main__":
    main(Path(sys.argv[1]))
