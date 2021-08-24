from pyinfra.operations import files, git

from bilder.components.baseline.steps import install_baseline_packages
from bilder.images.edxapp.lib import WEB_NODE_TYPE, node_type

install_baseline_packages(
    packages=[
        "curl",
        "git",
        "libmariadbclient-dev",
        "python3-pip",
        "python3-venv",
        "python3-dev",
        "build-essential",
    ],
    upgrade_system=True,
)

if node_type == WEB_NODE_TYPE:
    files.directory(
        name="Ensure themes directory is present",
        path="/edx/app/edxapp/themes/",
        present=True,
    )
    git.repo(
        name="Load theme repository",
        src="https://github.com/mitodl/mitxonline-theme",
        # Using a generic directory to simplify usage across deployments
        dest="/edx/app/edxapp/themes/edxapp-theme",
        branch="main",
    )
