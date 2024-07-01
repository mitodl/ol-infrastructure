import tempfile
from pathlib import Path

import httpx
from pyinfra import host
from pyinfra.api import deploy
from pyinfra.facts.server import LinuxName
from pyinfra.operations import apt, files, server, systemd

from bilder.components.hashicorp.models import HashicorpProduct
from bilder.facts.system import DebianCpuArch, RedhatCpuArch
from bilder.lib.linux_helpers import linux_family


@deploy("Install Hashicorp Products")
def install_hashicorp_products(hashicorp_products: list[HashicorpProduct]):
    apt.packages(
        name="Ensure unzip is installed",
        packages=["unzip"],
        update=True,
    )
    for product in hashicorp_products:
        server.user(  # noqa: S604
            name=f"Create system user for {product.name}",
            user=product.name,
            system=True,
            shell="/bin/false",
        )
        if linux_family(host.get_fact(LinuxName)).lower == "debian":
            cpu_arch = host.get_fact(DebianCpuArch)
        elif linux_family(host.get_fact(LinuxName)).lower == "redhat":
            cpu_arch = host.get_fact(RedhatCpuArch)
        else:
            cpu_arch = "amd64"
        file_download = f"{product.name}_{product.version}_linux_{cpu_arch}.zip"
        file_hashes = (
            httpx.get(
                f"https://releases.hashicorp.com/{product.name}/{product.version}/{product.name}_{product.version}_SHA256SUMS"
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
            src=f"https://releases.hashicorp.com/{product.name}/{product.version}/{file_download}",
            dest=download_destination,
            sha256sum=file_hash_map[file_download],
        )
        server.shell(
            name=f"Unzip {product.name}",
            commands=[f"unzip -o {download_destination} -d {target_directory}"],
        )
        files.file(
            name=f"Ensure {product.name} binary is executable",
            path=str(Path(target_directory).joinpath(product.name)),
            assume_present=download_binary.changed,
            user=product.name,
            group=product.name,
            mode="755",
        )
        files.directory(
            name=f"Ensure configuration directory for {product.name}",
            path=str(
                product.configuration_directory or product.configuration_file.parent
            ),
            present=True,
            user=product.name,
            group=product.name,
            recursive=True,
        )
        if hasattr(product, "data_directory"):
            files.directory(
                name=f"Create data directory for {product.name}",
                path=str(product.data_directory),
                present=True,
                user=product.name,
                group=product.name,
                recursive=True,
            )


@deploy("Register Hashicorp Service")
def register_services(
    hashicorp_products: list[HashicorpProduct],
    start_services_immediately=True,  # noqa: FBT002
):
    for product in hashicorp_products:
        systemd_unit = files.template(
            name=f"Create service definition for {product.name}",
            dest=f"/usr/lib/systemd/system/{product.name}.service",
            src=str(
                Path(__file__)
                .resolve()
                .parent.joinpath("templates", f"{product.name}.service.j2")
            ),
            context=product.systemd_template_context,
        )
        systemd.service(
            name=f"Register service for {product.name}",
            service=product.name,
            running=start_services_immediately,
            enabled=True,
            daemon_reload=systemd_unit.changed,
        )


@deploy("Configure Hashicorp Products")
def configure_hashicorp_product(product: HashicorpProduct):
    put_results = []
    for fpath, file_contents in product.render_configuration_files():
        temp_src = tempfile.NamedTemporaryFile(delete=False, mode="w")
        temp_src.write(file_contents)
        put_results.append(
            files.put(
                name=f"Create configuration file {fpath} for {product.name}",
                src=temp_src.name,
                create_remote_dir=True,
                user=product.name,
                group=product.name,
                dest=str(fpath),
            )
        )
        temp_src.close()


# Helper function to allow configuring to follow the same pattern as installing
def configure_hashicorp_products(hashicorp_products: list[HashicorpProduct]):
    for product in hashicorp_products:
        configure_hashicorp_product(product)
