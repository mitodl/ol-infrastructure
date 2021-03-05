import httpx
from pyinfra.api import deploy
from pyinfra.operations import apt, files, server

from ol_configuration_management.components.hashicorp.models import HashicorpConfig
from ol_configuration_management.facts import system  # noqa: F401
from ol_configuration_management.lib.linux_helpers import linux_family


@deploy("Install Hashicorp Products")  # noqa: WPS210
def install_hashicorp_products(
    hashicorp_config: HashicorpConfig, state=None, host=None
):
    apt.packages(
        name="Ensure unzip is installed",
        packages=["unzip"],
        update=True,
        state=state,
        host=host,
    )
    for product in hashicorp_config.products:
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
            commands=[f"unzip {download_destination} -d /usr/local/bin/"],
            state=state,
            host=host,
        )
        files.file(
            name=f"Ensure {product.name} binary is executable",
            path=f"/usr/local/bin/{product.name}",
            assume_present=download_binary.changed,
            user=product.name,
            group=product.name,
            mode="755",
            state=state,
            host=host,
        )
