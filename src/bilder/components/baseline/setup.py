from typing import List

from pyinfra.api import deploy
from pyinfra.operations import apt


@deploy("Install baseline requirements")
def install_baseline_packages(
    packages: List[str] = None, state=None, host=None, sudo=True
):
    apt.packages(
        name="Install baseline packages for Debian based hosts",
        packages=packages or ["curl"],
        update=True,
        state=state,
        host=host,
        sudo=sudo,
    )
