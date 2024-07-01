from io import StringIO
from pathlib import Path

import httpx
import yaml
from pyinfra import host
from pyinfra.api import deploy
from pyinfra.facts.server import LinuxName
from pyinfra.operations import files, server, systemd

from bilder.components.traefik.models.component import TraefikConfig
from bilder.facts.system import DebianCpuArch, RedhatCpuArch
from bilder.lib.linux_helpers import linux_family


def _ensure_traefik_user(traefik_config: TraefikConfig):
    server.user(  # noqa: S604
        name="Create system user for Traefik",
        user=traefik_config.user,
        system=True,
        shell="/bin/false",
    )


@deploy("Install traefik")
def install_traefik_binary(traefik_config: TraefikConfig):
    _ensure_traefik_user(traefik_config)
    if linux_family(host.get_fact(LinuxName)).lower == "debian":
        cpu_arch = host.get_fact(DebianCpuArch)
    elif linux_family(host.get_fact(LinuxName)).lower == "redhat":
        cpu_arch = host.get_fact(RedhatCpuArch)
    else:
        cpu_arch = "amd64"
    file_download = f"traefik_v{traefik_config.version}_linux_{cpu_arch}.tar.gz"
    file_hashes = (
        httpx.get(
            f"https://github.com/traefik/traefik/releases/download/v{traefik_config.version}/traefik_v{traefik_config.version}_checksums.txt",
            follow_redirects=True,
        )
        .read()
        .decode("utf8")
        .strip("\n")
        .split("\n")
    )
    file_hash_map = {
        file_hash.split()[1]: file_hash.split()[0] for file_hash in file_hashes
    }
    download_destination = "/tmp/traefik.tar.gz"  # noqa: S108
    target_directory = "/usr/local/bin/"
    download_binary = files.download(
        name="Download Traefik archive",
        src=f"https://github.com/traefik/traefik/releases/download/v{traefik_config.version}/{file_download}",
        dest=download_destination,
        sha256sum=file_hash_map[file_download],
    )
    server.shell(
        name="Unpack Traefik",
        commands=[f"tar -xvzf {download_destination} -C {target_directory}"],
    )
    files.file(
        name="Ensure Traefik binary is executable",
        path=str(Path(target_directory).joinpath("traefik")),
        assume_present=download_binary.changed,
        user=traefik_config.user,
        group=traefik_config.group,
        mode="755",
    )
    files.directory(
        name="Ensure configuration directory for Traefik",
        path=str(traefik_config.configuration_directory),
        present=True,
        user=traefik_config.user,
        group=traefik_config.group,
        recursive=True,
    )


@deploy("Configure traefik")
def configure_traefik(traefik_config: TraefikConfig):
    _ensure_traefik_user(traefik_config)
    files.put(
        name="Write Traefik static configuration file",
        src=StringIO(
            yaml.safe_dump(
                traefik_config.static_configuration.model_dump(
                    exclude_unset=True, by_alias=True
                )
            )
        ),
        dest=str(
            traefik_config.configuration_directory.joinpath(
                traefik_config.static_configuration_file
            )
        ),
        user=traefik_config.user,
    )
    for fpath, file_config in (traefik_config.file_configurations or {}).items():
        files.put(
            name=f"Write Traefik dynamic configuration file {fpath}",
            dest=str(traefik_config.configuration_directory.joinpath(fpath)),
            src=StringIO(
                yaml.safe_dump(
                    file_config.model_dump(exclude_unset=True, by_alias=True)
                )
            ),
            user=traefik_config.user,
        )


@deploy("Manage traefik service")
def traefik_service(
    traefik_config: TraefikConfig,
    do_restart=False,  # noqa: FBT002
    do_reload=False,  # noqa: FBT002
    start_immediately=False,  # noqa: FBT002
):
    systemd_unit = files.template(
        name="Create service definition for Traefik",
        dest="/usr/lib/systemd/system/traefik.service",
        src=str(
            Path(__file__).resolve().parent.joinpath("templates", "traefik.service.j2")
        ),
        context=traefik_config,
    )
    systemd.service(
        name="Enable traefik service",
        service="traefik",
        running=start_immediately,
        enabled=True,
        restarted=do_restart,
        reloaded=do_reload,
        daemon_reload=systemd_unit.changed,
    )
