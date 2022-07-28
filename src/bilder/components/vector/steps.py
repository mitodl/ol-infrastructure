from pyinfra.api import deploy
from pyinfra.operations import files, server, systemd

from bilder.components.vector.models import VectorConfig


def _debian_pkg_repo():
    debian_setup_script_path = "/tmp/vector_debian_setup.sh"
    files.download(
        name="Download Debian package setup script",
        src="https://repositories.timber.io/public/vector/setup.deb.sh",
        dest=debian_setup_script_path,
        mode="755",
    )
    server.shell(
        name="Set up Debian package repository",
        commands=[debian_setup_script_path],
    )


def _install_from_package():
    server.packages(
        name="Install Curl for Vector repo setup script",
        packages=["curl"],
        present=True,
    )
    _debian_pkg_repo()
    server.packages(
        name="Install Vector package",
        packages=["vector"],
        present=True,
    )
    files.directory(
        name="Remove example configurations",
        path="/etc/vector/examples/",
        assume_present=True,
        present=False,
    )
    files.file(
        name="Remove example vector.toml",
        path="/etc/vector/vector.toml",
        assume_present=True,
        present=False,
    )


@deploy("Install vector")
def install_vector(vector_config: VectorConfig):
    install_method_map = {"package": _install_from_package}
    install_method_map[vector_config.install_method]()
    if vector_config.is_proxy:
        files.directory(
            name="Ensure TLS config directory exists",
            path=str(vector_config.tls_config_directory),
            user=vector_config.user,
            present=True,
        )
    # Make sure you install vector AFTER installing docker when applicable.
    if vector_config.is_docker:
        server.shell(
            name="Add vector user to docker group",
            commands=[f"/usr/bin/gpasswd -a {vector_config.user} docker"],
        )


@deploy("Configure Vector")
def configure_vector(vector_config: VectorConfig):
    for fpath, context in vector_config.configuration_templates.items():
        files.template(
            name=f"Upload Vector configuration file {fpath}",
            src=str(fpath),
            dest=str(
                vector_config.configuration_directory.joinpath(
                    fpath.name.removesuffix(".j2")
                )
            ),
            user=vector_config.user,
            context=context,
        )

    # Validate the vector configuration files that were laid down
    # and confirm that vector starts without issue.
    server.shell(
        name="Run vector validate",
        commands=["/usr/bin/vector validate --no-environment"],
        _env={
            "VECTOR_CONFIG_DIR": "/etc/vector",
            "AWS_REGION": "us-east-1",
            "ENVIRONMENT": "placeholder",
            "GRAFANA_CLOUD_API_KEY": "placeholder",  # pragma: allowlist secret
            "HOSTNAME": "placeholder",
            "HEROKU_PROXY_PASSWORD": "placeholder",  # pragma: allowlist secret
            "HEROKU_PROXY_USERNAME": "placeholder",
            "APPLICATION": "placeholder",
        },
    )


@deploy("Manage Vector service")
def vector_service(
    vector_config: VectorConfig,
    do_restart=False,
    do_reload=False,
    start_immediately=False,
):
    systemd.service(
        name="Enable vector service",
        service="vector",
        running=start_immediately,
        enabled=True,
        restarted=do_restart,
        reloaded=do_reload,
    )
