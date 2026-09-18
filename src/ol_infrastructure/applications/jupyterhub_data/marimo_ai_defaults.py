"""Merge platform AI defaults into a user's marimo config at server start.

Shipped into single-user pods and run from the postStart hook, not imported by
the Pulumi program. marimo reads a single user config file from the persistent
home volume and writes a full default one the first time it starts, so seeding
a file with ``cp -n`` would never reach existing users. This fills in only the
keys a user has not set.

Each version of the defaults is applied once, recorded by its hash in a marker
file next to the config. Without that, a model the user removed from
``custom_models`` would come back on every restart. When the defaults change,
they're applied again, still without overriding a key the user has set.

``tomlkit`` is a marimo dependency, so it is present wherever marimo is.
"""

import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

import tomlkit

MARKER_NAME = ".ol-ai-defaults-applied"


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
    """Merge the defaults at ``defaults_path`` into the user's marimo config.

    :param defaults_path: A JSON file of marimo config defaults.
    :type defaults_path: Path
    """
    raw_defaults = defaults_path.read_bytes()
    defaults_hash = hashlib.sha256(raw_defaults).hexdigest()
    config_path = user_config_path()
    marker = config_path.parent / MARKER_NAME
    if marker.is_file() and marker.read_text().strip() == defaults_hash:
        return
    config_path.parent.mkdir(parents=True, exist_ok=True)
    document = (
        tomlkit.parse(config_path.read_text())
        if config_path.exists()
        else tomlkit.document()
    )
    merge_missing(document, json.loads(raw_defaults))
    write_atomically(config_path, tomlkit.dumps(document))
    write_atomically(marker, defaults_hash)


if __name__ == "__main__":
    main(Path(sys.argv[1]))
