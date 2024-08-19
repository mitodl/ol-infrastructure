# ruff: noqa: E501, S604
import json
import os
from io import StringIO
from pathlib import Path

from pyinfra import host
from pyinfra.operations import files, server

from bilder.components.hashicorp.consul.models import (
    Consul,
    ConsulAddresses,
    ConsulConfig,
)
from bilder.components.hashicorp.consul.steps import proxy_consul_dns
from bilder.components.hashicorp.steps import (
    configure_hashicorp_product,
    install_hashicorp_products,
    register_services,
)
from bilder.components.hashicorp.vault.models import (
    Vault,
    VaultAgentCache,
    VaultAgentConfig,
    VaultAutoAuthAWS,
    VaultAutoAuthConfig,
    VaultAutoAuthFileSink,
    VaultAutoAuthMethod,
    VaultAutoAuthSink,
    VaultConnectionConfig,
    VaultListener,
    VaultTCPListener,
    VaultTemplate,
)
from bilder.components.hashicorp.vault.steps import vault_template_permissions
from bilder.components.vector.models import VectorConfig
from bilder.components.vector.steps import (
    configure_vector,
    install_vector,
    vector_service,
)
from bilder.facts.has_systemd import HasSystemd
from bilder.lib.ami_helpers import build_tags_document
from bridge.lib.magic_numbers import VAULT_HTTP_PORT
from bridge.lib.versions import (
    APISIX_CLOUD_CLI_VERSION,
    APISIX_VERSION,
    CONSUL_VERSION,
    TRAEFIK_VERSION,
    VAULT_VERSION,
)
from bridge.secrets.sops import set_env_secrets

VERSIONS = {
    "consul": os.environ.get("CONSUL_VERSION", CONSUL_VERSION),
    "vault": os.environ.get("VAULT_VERSION", VAULT_VERSION),
    "traefik": os.environ.get("TRAEFIK_VERSION", TRAEFIK_VERSION),
    "apisix": os.environ.get("APISIX_VERSION", APISIX_VERSION),
    "apisix-cloud-cli": os.environ.get(
        "APISIX_CLOUD_CLI_VERSION", APISIX_CLOUD_CLI_VERSION
    ),
}
TEMPLATES_DIRECTORY = Path(__file__).parent.joinpath("templates")
FILES_DIRECTORY = Path(__file__).parent.joinpath("files")

# Set up configuration objects
set_env_secrets(Path("consul/consul.env"))
consul_configuration = {
    Path("00-default.json"): ConsulConfig(
        addresses=ConsulAddresses(dns="127.0.0.1", http="127.0.0.1"),
        advertise_addr='{{ GetInterfaceIP "ens5" }}',
        services=[],
    )
}
vector_config = VectorConfig(is_proxy=False)
vault_config = VaultAgentConfig(
    cache=VaultAgentCache(use_auto_auth_token="force"),  # noqa: S106
    listener=[
        VaultListener(
            tcp=VaultTCPListener(
                address=f"127.0.0.1:{VAULT_HTTP_PORT}", tls_disable=True
            )
        )
    ],
    vault=VaultConnectionConfig(
        address=f"https://vault.query.consul:{VAULT_HTTP_PORT}",
        tls_skip_verify=True,
    ),
    auto_auth=VaultAutoAuthConfig(
        method=VaultAutoAuthMethod(
            type="aws",
            mount_path="auth/aws",
            config=VaultAutoAuthAWS(role="apisix-gateway"),
        ),
        sink=[VaultAutoAuthSink(type="file", config=[VaultAutoAuthFileSink()])],
    ),
    template=[
        # Puts the token needed for talking to api7 where the cloud-cli service expects it
        VaultTemplate(
            contents='{{ with secret "secret-operations/apisix" }}API7_ACCESS_TOKEN={{ .Data.api7_access_token }}{{ end }}\n'
            f'APISIX_VERSION={VERSIONS["apisix"]}',
            destination=Path("/etc/default/cloud-cli"),
        )
    ],
    restart_period="5d",
    restart_jitter="12h",
)
vault = Vault(
    version=VERSIONS["vault"],
    configuration={Path("vault.json"): vault_config},
)
consul = Consul(version=VERSIONS["consul"], configuration=consul_configuration)

hashicorp_products = [vault, consul]
install_hashicorp_products(hashicorp_products)

server.user(
    name="Create apisix user.",
    user="apisix",
    groups=["docker"],
    home="/home/apisix",
    create_home=True,
    system=False,
    ensure_home=True,
    shell="/bin/bash",
    uid="1001",
)

# Download and install cloud-cli
files.download(
    name="Download the apisix cloud-cli binary",
    src=f"https://github.com/api7/cloud-cli/releases/download/{VERSIONS['apisix-cloud-cli']}/cloud-cli-linux-amd64-{VERSIONS['apisix-cloud-cli']}.gz",
    dest=f"/opt/cloud-cli-linux-amd64-{VERSIONS['apisix-cloud-cli']}.gz",
    mode="0644",
)

server.shell(
    name="Install apisix cloud-cli binary",
    commands=[
        f"/usr/bin/gunzip -c /opt/cloud-cli-linux-amd64-{VERSIONS['apisix-cloud-cli']}.gz > /usr/local/bin/cloud-cli",
        "chmod a+x /usr/local/bin/cloud-cli",
    ],
)

files.put(
    name="Place the cloud-cli-configure service definition.",
    src=str(FILES_DIRECTORY.joinpath("cloud-cli-configure.service")),
    dest="/usr/lib/systemd/system/cloud-cli-configure.service",
    mode="644",
)
files.put(
    name="Place the cloud-cli-deploy service definition.",
    src=str(FILES_DIRECTORY.joinpath("cloud-cli-deploy.service")),
    dest="/usr/lib/systemd/system/cloud-cli-deploy.service",
    mode="644",
)
files.put(
    name="Place supplemental configuration file.",
    src=str(FILES_DIRECTORY.joinpath("supplemental_config.yaml")),
    dest="/etc/docker/config.yaml",
    mode="644",
)


vault_template_permissions(vault_config)

# Install vector
install_vector(vector_config)
configure_vector(vector_config)

# Lay down final configuration for hashicorp products
for product in hashicorp_products:
    configure_hashicorp_product(product)

# Place the tags document
tags_json = json.dumps(
    build_tags_document(
        source_tags={
            "consul_version": VERSIONS["consul"],
            "vault_version": VERSIONS["vault"],
            "traefik_version": VERSIONS["traefik"],
            "apisix_version": VERSIONS["apisix"],
            "apisix_cloud_cli_version": VERSIONS["apisix-cloud-cli"],
        }
    )
)
files.put(
    name="Place the tags document at /etc/ami_tags.json",
    src=StringIO(tags_json),
    dest="/etc/ami_tags.json",
    mode="0644",
    user="root",
)

# Setup systemd daemons for everything
if host.get_fact(HasSystemd):
    vector_service(vector_config)

    register_services(hashicorp_products, start_services_immediately=False)
    proxy_consul_dns()

    server.service(
        name="Ensure docker compose service is disabled",
        service="docker-compose",
        enabled=False,  # We won't actually use docker-compose to run this service
        running=False,
    )

    server.service(
        name="Ensure the cloud-cli-configure service is enabled",
        service="cloud-cli-configure",
        enabled=True,
        running=False,
    )
    server.service(
        name="Ensure the cloud-cli-deploy service is enabled",
        service="cloud-cli-deploy",
        enabled=True,
        running=False,
    )
