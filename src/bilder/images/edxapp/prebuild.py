from pyinfra.operations import files, git, server

from bilder.components.baseline.steps import install_baseline_packages
from bilder.images.edxapp.lib import WEB_NODE_TYPE, node_type

EDX_USER = "edxapp"

install_baseline_packages(
    packages=[
        "build-essential",
        "curl",
        "git",
        "libmariadbclient-dev",
        "python3-dev",
        "python3-pip",
        "python3-venv",
        "python3-wheel",
    ],
    upgrade_system=True,
)

if node_type == WEB_NODE_TYPE:
    server.user(
        name="Proactively create edxapp user for setting permissions on theme repo",
        user=EDX_USER,
        present=True,
        shell="/bin/false",  # noqa: S604
    )
    files.directory(
        name="Ensure themes directory is present",
        path="/edx/app/edxapp/themes/",
        user=EDX_USER,
        group=EDX_USER,
        present=True,
    )
    git.repo(
        name="Load theme repository",
        src="https://github.com/mitodl/mitxonline-theme",
        # Using a generic directory to simplify usage across deployments
        dest="/edx/app/edxapp/themes/edxapp-theme",
        branch="main",
        user=EDX_USER,
        group=EDX_USER,
    )
