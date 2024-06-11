# ruff: noqa: E501
from pathlib import Path

from pyinfra import host
from pyinfra.api import deploy
from pyinfra.operations import apt, files, server, systemd

from bilder.components.alloy.models import AlloyConfig
from bilder.facts.has_systemd import HasSystemd


def _debian_pkg_repo(alloy_config: AlloyConfig):
    apt.packages(
        name="Install gpg package",
        packages=["gpg"],
        present=True,
    )

    server.shell(
        name="Setup grafana apt gpg key",
        commands=[
            f"wget -q -O - {alloy_config.gpg_key_url} | gpg --dearmor | sudo tee {alloy_config.keyring_path} > /dev/null"
        ],
    )
    server.shell(
        name="Add apt.grafana.com to the sources list.",
        commands=[
            f'echo "deb [signed-by={alloy_config.keyring_path}] {alloy_config.apt_repo_url} stable main" | sudo tee {alloy_config.sources_list_path}'
        ],
    )


def _install_from_package(alloy_config: AlloyConfig):
    _debian_pkg_repo(alloy_config)
    apt.packages(
        name="Install Alloy package",
        packages=["alloy"],
        present=True,
        update=True,
    )
    if alloy_config.clear_default_config:
        files.directory(
            name="Remove example configurations",
            path=f"{alloy_config.configuration_directory}/config.alloy",
            assume_present=True,
            present=False,
        )


# Wrapper function to do everything in one call.
@deploy("Install and configure Alloy.")
def install_and_configure_alloy(alloy_config: AlloyConfig):
    install_alloy(alloy_config)
    configure_alloy(alloy_config)
    if host.get_fact(HasSystemd):
        alloy_service(alloy_config)


@deploy("Install Alloy.")
def install_alloy(alloy_config: AlloyConfig):
    install_method_map = {"apt": _install_from_package}
    install_method_map[alloy_config.install_method](alloy_config)


@deploy("Configure Alloy.")
def configure_alloy(alloy_config: AlloyConfig):
    # At the moment alloy only supports the one configuration file
    # /etc/alloy/config.alloy
    # and it is not currently templated
    files.put(
        name=f"Create alloy configuration file {alloy_config.configuration_file}",
        src=str(Path(__file__).resolve().parent.joinpath("files", "config.alloy")),
        dest=str(alloy_config.configuration_file),
        user=alloy_config.user,
        group=alloy_config.user,
        mode="0644",
    )


@deploy("Configure Alloy: Setup systemd service")
def alloy_service(
    alloy_config: AlloyConfig,  # noqa: ARG001
    do_restart=False,  # noqa: FBT002
    do_reload=False,  # noqa: FBT002
    start_immediately=False,  # noqa: FBT002
):
    systemd.service(
        name="Enable alloy service",
        service="alloy",
        running=start_immediately,
        enabled=True,
        restarted=do_restart,
        reloaded=do_reload,
    )
