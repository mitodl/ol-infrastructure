from pathlib import Path

from pyinfra.api import deploy
from pyinfra.operations import apt, files, systemd


@deploy("Install baseline requirements")
def install_baseline_packages(
    packages: list[str] | None = None,
    upgrade_system: bool = False,  # noqa: FBT001, FBT002
):
    apt.packages(
        name="Install baseline packages for Debian based hosts",
        packages=packages or ["curl"],
        update=True,
        upgrade=upgrade_system,
    )


@deploy("Reload services on config change")
def service_configuration_watches(
    service_name: str,
    watched_files: list[Path],
    onchange_command: str | None = None,
    start_now: bool = True,  # noqa: FBT001, FBT002
):
    onchange_command = (
        onchange_command or f"/usr/bin/systemctl restart {service_name}.service"
    )
    restart_unit = files.template(
        name=f"Create {service_name} restarting service",
        dest=str(
            Path("/etc/systemd/system/").joinpath(f"{service_name}-restarter.service")
        ),
        src=str(
            Path(__file__)
            .resolve()
            .parent.joinpath("templates", "service_restarter.service.j2")
        ),
        service_name=service_name,
        onchange_command=onchange_command,
    )
    path_unit = files.template(
        name=f"Create {service_name} configuration file watcher",
        dest=str(Path("/etc/systemd/system").joinpath(f"{service_name}.path")),
        src=str(
            Path(__file__)
            .resolve()
            .parent.joinpath("templates", "systemd_file_watcher.path.j2")
        ),
        service_name=service_name,
        watched_files=watched_files,
    )
    systemd.service(
        name=f"Enable {service_name} configuration file watcher",
        service=f"{service_name}.path",
        daemon_reload=path_unit.changed or restart_unit.changed,
        enabled=True,
        running=start_now,
    )
