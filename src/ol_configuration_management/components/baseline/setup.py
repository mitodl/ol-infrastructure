from pyinfra.api import deploy
from pyinfra.operations import apt


@deploy("Install baseline requirements")
def install_baseline_packages(state=None, host=None):
    apt.packages(
        name="Install baseline packages for Debian based hosts",
        packages=[
            "curl",
        ],
        update=True,
        state=state,
        host=host,
    )
