import tempfile
from pathlib import Path
from typing import List

import httpx
from pyinfra.api import deploy
from pyinfra.operations import apt, files, server, systemd

from bilder.components.hashicorp.models import HashicorpProduct
from bilder.facts import has_systemd, system  # noqa: F401
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
        if linux_family(host.fact.linux_name).lower == "debian":
            cpu_arch = host.fact.debian_cpu_arch
        elif linux_family(host.fact.linux_name).lower == "redhat":
            cpu_arch = host.fact.redhat_cpu_arch
        else:
            cpu_arch = "amd64"
        file_download = f"{product.name}_{product.version}_linux_{cpu_arch}.zip"
        file_hashes = (
            httpx.get(
                "https://releases.hashicorp.com/{product_name}/{product_version}/{product_name}_{product_version}_SHA256SUMS".format(  # noqa: E501
                    product_name=product.name, product_version=product.version
                )
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
            src=f"https://releases.hashicorp.com/{product.name}/{product.version}/{file_download}",  # noqa: WPS221,E501
            dest=download_destination,
            sha256sum=file_hash_map[file_download],
            state=state,
            host=host,
        )
        server.shell(
            name=f"Unzip {product.name}",
            commands=[f"unzip -o {download_destination} -d {target_directory}"],
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
        files.directory(
            name=f"Ensure configuration directory for {product.name}",
            path=product.configuration_directory or product.configuration_file.parent,
            present=True,
            user=product.name,
            group=product.name,
            recursive=True,
            state=state,
            host=host,
        )
        if hasattr(product, "data_directory"):  # noqa: WPS421
            files.directory(
                name=f"Create data directory for {product.name}",
                path=product.data_directory,
                present=True,
                user=product.name,
                group=product.name,
                recursive=True,
                state=state,
                host=host,
            )


@deploy("Register Hashicorp Service")
def register_services(
    hashicorp_products: List[HashicorpProduct], state=None, host=None
):
    for product in hashicorp_products:
        systemd_unit = files.template(
            name=f"Create service definition for {product.name}",
            dest=f"/usr/lib/systemd/system/{product.name}.service",
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
def configure_hashicorp_product(product: HashicorpProduct, state=None, host=None):
    put_results = []
    for fpath, file_contents in product.render_configuration_files():
        temp_src = tempfile.NamedTemporaryFile(delete=False)
        temp_src.write(file_contents.encode("utf8"))
        put_results.append(
            files.put(
                name=f"Create configuration file {fpath} for {product.name}",
                src=temp_src.name,
                create_remote_dir=True,
                user=product.name,
                group=product.name,
                dest=fpath,
                state=state,
                host=host,
            )
        )
    if host.fact.has_systemd:
        systemd.service(
            name=f"Reload service for {product.name}",
            service=product.name,
            reloaded=any(upload_result.changed for upload_result in put_results),
            host=host,
            state=state,
        )
