# ruff: noqa: E501

from pyinfra import host
from pyinfra.api import deploy
from pyinfra.operations import files, server, systemd

from bilder.components.alloy.models import AlloyConfig
from bilder.facts.has_systemd import HasSystemd


def _debian_pkg_repo(alloy_config: AlloyConfig):
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
    server.packages(
        name="Install Alloy package",
        packages=["alloy"],
        present=True,
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
    for fpath, context in alloy_config.configuration_templates.items():
        files.template(
            name=f"Upload Alloy configuration file {fpath}",
            src=str(fpath),
            dest=str(
                alloy_config.configuration_directory.joinpath(
                    fpath.name.removesuffix(".j2")
                )
            ),
            user=alloy_config.user,
            context=context,
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
