from pyinfra.api import deploy
from pyinfra.operations import files, server, systemd

from bilder.components.vector.models import VectorConfig


def _debian_pkg_repo(state=None, host=None):
    debian_setup_script_path = "/tmp/vector_debian_setup.sh"  # noqa: S108
    files.download(
        name="Download Debian package setup script",
        src="https://repositories.timber.io/public/vector/setup.deb.sh",
        dest=debian_setup_script_path,
        mode="755",
        state=state,
        host=host,
    )
    server.shell(
        name="Set up Debian package repository",
        commands=[debian_setup_script_path],
        state=state,
        host=host,
    )


def _install_from_package(state=None, host=None):
    server.packages(
        name="Install Curl for Vector repo setup script",
        packages=["curl"],
        present=True,
        state=state,
        host=host,
    )
    _debian_pkg_repo(state=state, host=host)
    server.packages(
        name="Install Vector package",
        packages=["vector"],
        present=True,
        state=state,
        host=host,
    )
    files.directory(
        name="Remove example configurations",
        path="/etc/vector/examples/",
        assume_present=True,
        present=False,
        state=state,
        host=host,
    )
    files.file(
        name="Remove example vector.toml",
        path="/etc/vector/vector.toml",
        assume_present=True,
        present=False,
        state=state,
        host=host,
    )


@deploy("Install vector")
def install_vector(vector_config: VectorConfig, state=None, host=None):
    install_method_map = {"package": _install_from_package}
    install_method_map[vector_config.install_method](state, host)


@deploy("Configure Vector")
def configure_vector(vector_config: VectorConfig, state=None, host=None):
    for fpath, context in vector_config.configuration_templates.items():
        files.template(
            name=f"Upload Vector configuration file {fpath}",
            src=fpath,
            dest=vector_config.configuration_directory.joinpath(
                fpath.name.removesuffix(".j2")
            ),
            user=vector_config.user,
            context=context,
            state=state,
            host=host,
        )


@deploy("Validate vector configurations")
def validate_vector(state=None, host=None):
    server.shell(
        name="Run vector validate",
        commands=[
            "VECTOR_CONFIG_DIR=/etc/vector AWS_REGION=us-east-1 ENVIRONMENT=placeholder GRAFANA_CLOUD_API_KEY=placeholder /usr/bin/vector validate --no-environment"
        ],
        state=state,
        host=host,
    )


@deploy("Manage Vector service")
def vector_service(
    vector_config: VectorConfig,
    state=None,
    host=None,
    do_restart=False,
    do_reload=False,
):
    systemd.service(
        name="Enable vector service",
        service="vector",
        running=True,
        enabled=True,
        restarted=do_restart,
        reloaded=do_reload,
        state=state,
        host=host,
    )
