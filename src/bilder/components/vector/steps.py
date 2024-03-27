from pathlib import Path

from pyinfra import host
from pyinfra.api import deploy
from pyinfra.facts.files import File
from pyinfra.operations import files, server, systemd

from bilder.components.vector.models import VectorConfig
from bilder.facts.has_systemd import HasSystemd


def _debian_pkg_repo():
    debian_setup_script_path = "/tmp/vector_debian_setup.sh"  # noqa: S108
    files.download(
        name="Download Debian package setup script",
        src="https://setup.vector.dev",
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


# Wrapper function to do everything in one call.
@deploy("Install and configure vector.")
def install_and_configure_vector(vector_config: VectorConfig):
    install_vector(vector_config)
    configure_vector(vector_config)
    if host.get_fact(HasSystemd):
        vector_service(vector_config)


# TODO: MD 20230131 Deprecate calling install_vector,  # noqa: FIX002, TD002, TD003
# configure_vector, manage_service from outside the module.
# aka, make them private functions.
@deploy("Install vector: Install and determine shared configuration items.")
def install_vector(vector_config: VectorConfig):
    install_method_map = {"package": _install_from_package}
    install_method_map[vector_config.install_method]()

    # Running a vector_log_proxy server is special
    if vector_config.is_proxy:
        files.directory(
            name="Ensure TLS config directory exists",
            path=str(vector_config.tls_config_directory),
            user=vector_config.user,
            present=True,
        )
    if host.get_fact(File, "/lib/systemd/system/vector.service"):
        server.shell(
            name="Backup original vector.service defintion",
            commands=["/usr/bin/mv /lib/systemd/system/vector.service /root"],
        )
    files.put(
        dest="/lib/systemd/system/vector.service",
        group="root",
        mode="0644",
        name="Override original vector.service definition",
        src=str(Path(__file__).resolve().parent.joinpath("files", "vector.service")),
        user="root",
    )

    # Special permissions and configuration for running with dockerized services.
    # Make sure you install vector AFTER installing docker, when applicable.
    # TODO: MD 20230131 Split docker config out to its own private function.  # noqa: E501, FIX002, TD002, TD003
    if vector_config.is_docker:
        server.shell(
            name="Add vector user to docker group",
            commands=[f"/usr/bin/gpasswd -a {vector_config.user} docker"],
        )
        vector_config.configuration_templates[
            Path(__file__).resolve().parent.joinpath("templates", "docker_source.yaml")
        ] = {}

    # Config flags to enable global sink configurations
    if vector_config.use_global_log_sink:
        vector_config.configuration_templates[
            Path(__file__)
            .resolve()
            .parent.joinpath("templates", "global_log_sink.yaml")
        ] = {}
    if vector_config.use_global_metric_sink:
        vector_config.configuration_templates[
            Path(__file__)
            .resolve()
            .parent.joinpath("templates", "global_metric_sink.yaml")
        ] = {}


@deploy("Configure Vector: create configuration files")
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
    #
    # TODO MD 20230127 This won't work once we switch the sink config to be  # noqa: E501, FIX002, TD002, TD003, TD004
    # consul/vault templates. Need to come up with something else or remove
    # this entirely. Vector now offers unit tests so perhaps we can use that.
    server.shell(
        name="Run vector validate",
        commands=["/usr/bin/vector validate --no-environment --strict-env-vars=false"],
        _env={
            "VECTOR_CONFIG_DIR": "/etc/vector",
            "AWS_REGION": "us-east-1",
            "ENVIRONMENT": "placeholder",
            "APPLICATION": "placeholder",
            "SERVICE": "placeholder",
            "FASTLY_PROXY_PASSWORD": "placeholder",  # pragma: allowlist secret
            "FASTLY_PROXY_USERNAME": "placeholder",
            "GRAFANA_CLOUD_API_KEY": "placeholder",  # pragma: allowlist secret
            "HOSTNAME": "placeholder",
            "HEROKU_PROXY_PASSWORD": "placeholder",  # pragma: allowlist secret
            "HEROKU_PROXY_USERNAME": "placeholder",
        },
    )


@deploy("Configure Vector: Setup systemd service")
def vector_service(
    vector_config: VectorConfig,  # noqa: ARG001
    do_restart=False,  # noqa: FBT002
    do_reload=False,  # noqa: FBT002
    start_immediately=False,  # noqa: FBT002
):
    systemd.service(
        name="Enable vector service",
        service="vector",
        running=start_immediately,
        enabled=True,
        restarted=do_restart,
        reloaded=do_reload,
    )
