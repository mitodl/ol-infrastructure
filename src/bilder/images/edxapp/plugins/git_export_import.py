from pathlib import Path

from pyinfra.api import deploy
from pyinfra.operations import files, pip, ssh


@deploy("Set up git auto export")
def git_auto_export():
    pip.packages(
        name="Install edx-git-auto-export",
        packages=[
            "edx-git-auto-export",
        ],
        present=True,
        virtualenv="/edx/app/edxapp/venvs/edxapp/",
        sudo_user="edxapp",
    )

    files.directory(
        name="Create .ssh directory for www-data user to clone course repositories",
        path=Path("/var/www/.ssh/"),
        present=True,
        mode="0700",
        user="www-data",
        group="www-data",
    )
    for git_host in ("github.com", "github.mit.edu"):
        ssh.keyscan(
            name=f"Add {git_host} public SSH fingerprint for course import/export",
            hostname=git_host,
            sudo_user="www-data",
        )
