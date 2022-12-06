import os
from io import StringIO

from pyinfra.operations import files

from bridge.settings.openedx.types import OpenEdxSupportedRelease


def set_openedx_release_env():
    release_name: OpenEdxSupportedRelease = os.environ["OPENEDX_RELEASE"]
    files.put(
        name="Place the forum .env file",
        src=StringIO(f"OPENEDX_RELEASE={release_name}"),
        dest="/etc/defaults/openedx",
        mode="0444",
    )
