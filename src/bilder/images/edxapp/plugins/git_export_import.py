from pathlib import Path

from pyinfra.operations import files, pip, ssh

pip.packages(
    name="Install edx-git-auto-export",
    packages=[
        "git+https://github.com/mitodl/edx-git-auto-export.git@v0.3#egg=edx-git-auto-export",  # noqa: E501
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
for host in ("github.com", "github.mit.edu"):
    ssh.keyscan(
        name=f"Add {host} public SSH fingerprint for course import/export",
        hostname=host,
        su_user="www-data",
        su_shell="/bin/bash",
    )
