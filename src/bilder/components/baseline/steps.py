from pathlib import Path
from typing import List, Optional

from pyinfra.api import deploy
from pyinfra.operations import apt, files, systemd


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


@deploy("Reload services on config change")
def service_configuration_watches(
    service_name: str,
    watched_files: List[Path],
    onchange_command: Optional[str] = None,
    state=None,
    host=None,
):
    onchange_command = (
        onchange_command or f"/usr/bin/systemctl restart {service_name}.service"
    )
    restart_unit = files.template(
        name=f"Create {service_name} restarting service",
        dest=Path("/etc/systemd/system/").joinpath(f"{service_name}-restarter.service"),
        src=Path(__file__).parent.joinpath("templates", "service_restarter.service.j2"),
        service_name=service_name,
        onchange_command=onchange_command,
        state=state,
        host=host,
    )
    path_unit = files.template(
        name=f"Create {service_name} configuration file watcher",
        dest=Path("/etc/systemd/system").joinpath(f"{service_name}.path"),
        src=Path(__file__).parent.joinpath("templates", "systemd_file_watcher.path.j2"),
        service_name=service_name,
        watched_files=watched_files,
        state=state,
        host=host,
    )
    systemd.service(
        name=f"Enable {service_name} configuration file watcher",
        service=f"{service_name}.path",
        daemon_reload=path_unit.changed or restart_unit.changed,
        enabled=True,
        running=True,
        state=state,
        host=host,
    )
