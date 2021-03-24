from pathlib import Path
from typing import List

import httpx
from pyinfra.api import deploy
from pyinfra.operations import apt, files, server, systemd

from bilder.components.hashicorp.models import HashicorpConfig, HashicorpProduct
from bilder.facts import system  # noqa: F401
from bilder.lib.linux_helpers import linux_family


@deploy("Install Hashicorp Products")  # noqa: WPS210
def install_hashicorp_products(
    hashicorp_products: List[HashicorpProduct], state=None, host=None
):
    apt.packages(
        name="Ensure unzip is installed",
        packages=["unzip"],
        update=True,
        state=state,
        host=host,
    )
    for product in hashicorp_products:
        server.user(
            name=f"Create system user for {product.name}",
            user=product.name,
            system=True,
            shell="/bin/false",  # noqa: S604
            state=state,
            host=host,
        )
        # TODO: Remove the call to `.split` after
        # https://github.com/Fizzadar/pyinfra/pull/545 gets merged and released.
        # TMM 2021-03-05
        if linux_family(host.fact.linux_name.split()[0]).lower == "debian":
            cpu_arch = host.fact.debian_cpu_arch
        elif linux_family(host.fact.linux_name.split()[0]).lower == "redhat":
            cpu_arch = host.fact.redhat_cpu_arch
        else:
            cpu_arch = "amd64"
        file_download = f"{product.name}_{product.version}_linux_{cpu_arch}.zip"
        file_hashes = (
            httpx.get(
                f"https://releases.hashicorp.com/{product.name}/{product.version}/{product.name}_{product.version}_SHA256SUMS"  # noqa: WPS221
            )
            .read()
            .decode("utf8")
            .strip("\n")
            .split("\n")
        )
        file_hash_map = {
            file_hash.split()[1]: file_hash.split()[0] for file_hash in file_hashes
        }
        download_destination = f"/tmp/{product.name}.zip"  # noqa: S108
        target_directory = product.install_directory or "/usr/local/bin/"
        download_binary = files.download(
            name=f"Download {product.name} archive",
            src=f"https://releases.hashicorp.com/{product.name}/{product.version}/{file_download}",  # noqa: WPS221
            dest=download_destination,
            sha256sum=file_hash_map[file_download],
            state=state,
            host=host,
        )
        server.shell(
            name=f"Unzip {product.name}",
            commands=[f"unzip {download_destination} -d {target_directory}"],
            state=state,
            host=host,
        )
        files.file(
            name=f"Ensure {product.name} binary is executable",
            path=Path(target_directory).joinpath(product.name),
            assume_present=download_binary.changed,
            user=product.name,
            group=product.name,
            mode="755",
            state=state,
            host=host,
        )


@deploy("Register Hashicorp Service")
def register_service(hashicorp_products: List[HashicorpProduct], state=None, host=None):
    for product in hashicorp_products:
        systemd_unit = files.template(
            f"Create service definition for {product.name}",
            dest="/usr/lib/systemd/system/{product.name}.service",
            src=Path(__file__).parent.joinpath(
                "templates", f"{product.name}.service.j2"
            ),
            context=product.systemd_template_context,
            state=state,
            host=host,
        )
        systemd.service(
            name=f"Register service for {product.name}",
            service=product.name,
            running=True,
            enabled=True,
            daemon_reload=systemd_unit.changed,
            state=state,
            host=host,
        )


@deploy("Configure Hashicorp Products")
def configure_hashicorp_products(
    product_config: HashicorpConfig, state=None, host=None
):
    pass  # noqa: WPS420
