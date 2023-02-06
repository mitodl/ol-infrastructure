import json
import tempfile

from pyinfra.operations import apt, files, git, server

from bilder.components.baseline.steps import install_baseline_packages
from bilder.images.edxapp.lib import OPENEDX_RELEASE, WEB_NODE_TYPE, node_type
from bridge.settings.openedx.accessors import (
    fetch_application_version,
    filter_deployments_by_release,
)
from bridge.settings.openedx.types import OpenEdxApplication

EDX_USER = "edxapp"

apt.packages(
    name="Remove unattended-upgrades to prevent race conditions during build",
    packages=["unattended-upgrades"],
    present=False,
)

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

server.shell(
    name="Disable git safe directory checking on immutable machines",
    commands=["git config --system safe.directory *"],
)

if node_type == WEB_NODE_TYPE:
    server.user(
        name="Proactively create edxapp user for setting permissions on theme repo",
        user=EDX_USER,
        present=True,
        shell="/bin/false",
    )
    files.directory(
        name="Ensure themes directory is present",
        path="/edx/app/edxapp/themes/",
        user=EDX_USER,
        group=EDX_USER,
        present=True,
    )
    for deployment in filter_deployments_by_release(OPENEDX_RELEASE):
        theme = fetch_application_version(
            OPENEDX_RELEASE, deployment.deployment_name, OpenEdxApplication.theme
        )
        git.repo(
            name="Load theme repository",
            src=theme.git_origin,
            # Using a generic directory to simplify usage across deployments
            dest=f"/edx/app/edxapp/themes/{deployment.deployment_name}",
            branch=theme.release_branch,
            user=EDX_USER,
            group=EDX_USER,
        )
    with tempfile.NamedTemporaryFile(mode="wt", delete=False) as worker_config:
        worker_config.write(
            json.dumps(
                {
                    "edx-proctoring-proctortrack": [
                        "babel-polyfill",
                        "/edx/app/edxapp/edx-platform/node_modules/edx-proctoring-proctortrack/edx_proctoring_proctortrack/static/proctortrack_custom.js",  # noqa: E501
                    ]
                }
            )
        )
        files.put(
            name="Create workers.json file to enable proctortrack extension",
            src=worker_config.name,
            dest="/edx/app/edxapp/workers.json",
            user=EDX_USER,
            group=EDX_USER,
            create_remote_dir=True,
        )
