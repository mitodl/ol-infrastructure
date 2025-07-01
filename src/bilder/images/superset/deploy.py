import io
import os
from pathlib import Path

from pyinfra import host
from pyinfra.operations import files, server

from bilder.components.baseline.steps import service_configuration_watches
from bilder.components.hashicorp.consul.models import (
    Consul,
    ConsulAddresses,
    ConsulConfig,
)
from bilder.components.hashicorp.consul.steps import proxy_consul_dns
from bilder.components.hashicorp.steps import (
    configure_hashicorp_product,
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
)
from bilder.components.traefik.models import traefik_static
from bilder.components.traefik.models.component import TraefikConfig
from bilder.components.traefik.steps import configure_traefik
from bilder.components.vector.models import VectorConfig
from bilder.components.vector.steps import install_and_configure_vector
from bilder.facts.has_systemd import HasSystemd
from bilder.lib.linux_helpers import DOCKER_COMPOSE_DIRECTORY
from bridge.lib.magic_numbers import VAULT_HTTP_PORT
from bridge.lib.versions import CONSUL_VERSION, VAULT_VERSION
from bridge.secrets.sops import set_env_secrets

TEMPLATES_DIRECTORY = Path(__file__).resolve().parent.joinpath("templates")
FILES_DIRECTORY = Path(__file__).resolve().parent.joinpath("files")

watched_docker_compose_files = []

VERSIONS = {
    "consul": os.environ.get("CONSUL_VERSION", CONSUL_VERSION),
    "vault": os.environ.get("VAULT_VERSION", VAULT_VERSION),
}

SUPERSET_IMAGE_SHA = os.environ.get("SUPERSET_IMAGE_SHA")

set_env_secrets(Path("consul/consul.env"))

# Preload the superset docker image to accelerate startup
server.shell(
    name=f"Preload mitodl/superset@{SUPERSET_IMAGE_SHA}",
    commands=[
        f"/usr/bin/docker pull mitodl/superset@{SUPERSET_IMAGE_SHA}",
    ],
)

# There is only one key needed in the .env. Everything else will come at runtime
# via the helper entrypoint built into the custom superset image.
files.put(
    name="Setup .env file for docker compose.",
    src=io.StringIO(
        f"SUPERSET_IMAGE_SHA={SUPERSET_IMAGE_SHA}\nSUPERSET_HOME=/app/superset_home\n"
    ),
    dest=str(DOCKER_COMPOSE_DIRECTORY.joinpath(".env")),
)
watched_docker_compose_files.append(str(DOCKER_COMPOSE_DIRECTORY.joinpath(".env")))

files.put(
    name="Place docker-compose.yaml.",
    src=str(FILES_DIRECTORY.joinpath("docker-compose.yaml")),
    dest=str(DOCKER_COMPOSE_DIRECTORY.joinpath("docker-compose.yaml")),
    mode="0664",
)
watched_docker_compose_files.append(
    str(DOCKER_COMPOSE_DIRECTORY.joinpath("docker-compose.yaml"))
)

# Configure and install consul + vault
consul_configuration = {
    Path("00-default.json"): ConsulConfig(
        addresses=ConsulAddresses(dns="127.0.0.1", http="127.0.0.1"),
        advertise_addr="{{ GetPrivateIP }}",
    )
}
consul = Consul(version=VERSIONS["consul"], configuration=consul_configuration)

vault_configuration = VaultAgentConfig(
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
            config=VaultAutoAuthAWS(role="superset"),
        ),
        sink=[VaultAutoAuthSink(type="file", config=[VaultAutoAuthFileSink()])],
    ),
)
vault = Vault(
    version=VERSIONS["vault"],
    configuration={Path("vault.json"): vault_configuration},
)

hashicorp_products = [vault, consul]
for product in hashicorp_products:
    configure_hashicorp_product(product)

# Configure Traefik
traefik_config = TraefikConfig(
    static_configuration=traefik_static.TraefikStaticConfig(
        log=traefik_static.Log(format="json"),
        providers=traefik_static.Providers(docker=traefik_static.Docker()),
        certificates_resolvers={
            "letsencrypt_resolver": traefik_static.CertificatesResolvers(
                acme=traefik_static.Acme(
                    email="odl-devops@mit.edu",
                    storage="/etc/traefik/acme.json",
                    dns_challenge=traefik_static.DnsChallenge(provider="route53"),
                )
            )
        },
        entry_points={
            "http": traefik_static.EntryPoints(
                address=":80",
                http=traefik_static.Http(
                    redirections=traefik_static.Redirections(
                        entry_point=traefik_static.EntryPoint(
                            to="https",
                            scheme="https",
                            permanent=True,
                        )
                    )
                ),
            ),
            "https": traefik_static.EntryPoints(address=":443"),
        },
    ),
)
configure_traefik(traefik_config)

# Install and configure vector
vector_config = VectorConfig(is_docker=True, use_global_log_sink=True)
vector_config.configuration_templates[
    FILES_DIRECTORY.joinpath("vector", "superset_logs.yaml")
] = {}
install_and_configure_vector(vector_config)

if host.get_fact(HasSystemd):
    register_services(hashicorp_products, start_services_immediately=False)
    proxy_consul_dns()
    server.service(
        name="Ensure docker compose service is enabled",
        service="docker-compose",
        running=False,
        enabled=True,
    )
    service_configuration_watches(
        service_name="docker-compose", watched_files=watched_docker_compose_files
    )
