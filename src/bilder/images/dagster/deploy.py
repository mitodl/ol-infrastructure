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
from bilder.components.hashicorp.consul_template.models import (
    ConsulTemplate,
    ConsulTemplateConfig,
    ConsulTemplateTemplate,
    ConsulTemplateVaultConfig,
)
from bilder.components.hashicorp.consul_template.steps import (
    consul_template_permissions,
)
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
from bilder.components.hashicorp.vault.steps import vault_template_permissions
from bilder.components.vector.models import VectorConfig
from bilder.components.vector.steps import install_and_configure_vector
from bilder.facts.has_systemd import HasSystemd
from bilder.lib.linux_helpers import DOCKER_COMPOSE_DIRECTORY
from bridge.lib.magic_numbers import DEFAULT_HTTPS_PORT, VAULT_HTTP_PORT
from bridge.lib.versions import (
    CONSUL_TEMPLATE_VERSION,
    CONSUL_VERSION,
    VAULT_VERSION,
)
from bridge.secrets.sops import set_env_secrets

##################################################
# Globals and misc stuff

TEMPLATES_DIRECTORY = Path(__file__).resolve().parent.joinpath("templates")
FILES_DIRECTORY = Path(__file__).resolve().parent.joinpath("files")

VERSIONS = {
    "consul": os.environ.get("CONSUL_VERSION", CONSUL_VERSION),
    "consul-template": os.environ.get(
        "CONSUL_TEMPLATE_VERSION", CONSUL_TEMPLATE_VERSION
    ),
    "vault": os.environ.get("VAULT_VERSION", VAULT_VERSION),
}

DOCKER_REPO_NAME = os.environ.get("DOCKER_REPO_NAME", "mitodl/mono-dagster")
DOCKER_IMAGE_DIGEST = os.environ.get("DOCKER_IMAGE_DIGEST")

set_env_secrets(Path("consul/consul.env"))

server.user(
    name="Create the dagster user.",
    user="dagster",
    system=True,
    ensure_home=False,
)

files.put(
    name="Set AWS config file for use with STS assume role credentials",
    src=FILES_DIRECTORY.joinpath("aws_config.ini"),
    dest="/etc/aws/config",
    create_remote_dir=True,
)

watched_docker_compose_files = []

consul_templates_directory = Path("/etc/consul-template")
consul_templates = []

# Preload the dagster image to accelerate the startup
server.shell(
    name=f"Preload {DOCKER_REPO_NAME}@{DOCKER_IMAGE_DIGEST}",
    commands=[
        f"/usr/bin/docker pull {DOCKER_REPO_NAME}@{DOCKER_IMAGE_DIGEST}",
    ],
)

##################################################
# Put down EDX pipeline consul templates
edx_pipeline_files = [
    "edxorg_gcp.yaml",
]

edx_pipeline_directory = Path("/opt/pipeline_definitions/edx_pipeline")
files.directory(
    name="Create edx-pipeline directory",
    path=str(edx_pipeline_directory),
    user="dagster",
    group="dagster",
    present=True,
)
for edx_pipeline in edx_pipeline_files:
    files.put(
        name=f"Place edx-pipeline: {edx_pipeline}.tmpl",
        src=str(
            FILES_DIRECTORY.joinpath(f"pipelines/edx-pipeline/{edx_pipeline}.tmpl")
        ),
        dest=str(consul_templates_directory.joinpath(f"{edx_pipeline}.tmpl")),
        mode="0664",
    )
    consul_templates.append(
        ConsulTemplateTemplate(
            source=str(consul_templates_directory.joinpath(f"{edx_pipeline}.tmpl")),
            destination=str(edx_pipeline_directory.joinpath(edx_pipeline)),
        )
    )
    watched_docker_compose_files.append(
        str(edx_pipeline_directory.joinpath(edx_pipeline))
    )

##################################################
# Place Traefik configuration items
traefik_conf_directory = Path("/etc/traefik")
traefik_conf_template_file = consul_templates_directory.joinpath(
    f"traefik.yaml.tmpl"  # noqa: F541
)
traefik_conf_file = traefik_conf_directory.joinpath("traefik.yaml")

certificate_file = traefik_conf_directory.joinpath("star.odl.mit.edu.crt")
certificate_key_file = traefik_conf_directory.joinpath("star.odl.mit.edu.key")

files.directory(
    name="Create Traefik directory",
    path=str(traefik_conf_directory),
    user="root",
    group="root",
    present=True,
)
files.put(
    name="Create the Traefik configuration consul-template file.",
    src=str(FILES_DIRECTORY.joinpath("traefik.yaml.tmpl")),
    dest=str(traefik_conf_template_file),
    mode="0664",
    user="root",
    group="root",
)
consul_templates.append(
    ConsulTemplateTemplate(
        source=str(traefik_conf_template_file),
        destination=str(traefik_conf_file),
        user="root",
        group="root",
    )
)
watched_docker_compose_files.append(str(traefik_conf_file))

consul_templates.extend(
    [
        ConsulTemplateTemplate(
            contents=(
                '{{ with secret "secret-global/odl-wildcard" }}'
                "{{ printf .Data.data.key_with_proper_newlines }}{{ end }}"
            ),
            destination=Path(certificate_key_file),
            user="root",
            group="root",
        ),
        ConsulTemplateTemplate(
            contents=(
                '{{ with secret "secret-global/odl-wildcard" }}'
                "{{ printf .Data.data.cert_with_proper_newlines }}{{ end }}"
            ),
            destination=Path(certificate_file),
            user="root",
            group="root",
        ),
    ]
)
watched_docker_compose_files.extend([str(certificate_file), str(certificate_key_file)])

##################################################
# Put down the docker compose configurations + .env
docker_compose_context = {
    "docker_repo_name": DOCKER_REPO_NAME,
    "docker_image_digest": DOCKER_IMAGE_DIGEST,
    "edx_pipeline_definition_directory": edx_pipeline_directory,
    "listener_port": DEFAULT_HTTPS_PORT,
    "certificate_file": certificate_file,
    "certificate_key_file": certificate_key_file,
    "traefik_directory": traefik_conf_directory,
}
files.template(
    name="Place the dagster docker-compose.yaml file",
    src=str(TEMPLATES_DIRECTORY.joinpath("docker-compose.yaml.j2")),
    dest=str(DOCKER_COMPOSE_DIRECTORY.joinpath("docker-compose.yaml")),
    context=docker_compose_context,
    mode="0664",
)
watched_docker_compose_files.append(
    str(DOCKER_COMPOSE_DIRECTORY.joinpath("docker-compose.yaml"))
)

files.put(
    name="Place the dagster .env file.",
    src=str(FILES_DIRECTORY.joinpath(f".env.tmpl")),  # noqa: F541
    dest=str(consul_templates_directory.joinpath(f".env.tmpl")),  # noqa: F541
    mode="0664",
)

files.put(
    name="Place the traefik-forward-auth .env file.",
    src=str(FILES_DIRECTORY.joinpath(f".env_traefik_forward_auth.tmpl")),  # noqa: F541
    dest=str(consul_templates_directory.joinpath(".env_traefik_forward_auth.tmpl")),
    mode="0664",
)

consul_templates.append(
    ConsulTemplateTemplate(
        source=str(consul_templates_directory.joinpath(f".env.tmpl")),  # noqa: F541
        destination=str(DOCKER_COMPOSE_DIRECTORY.joinpath(".env")),
    )
)
watched_docker_compose_files.append(str(DOCKER_COMPOSE_DIRECTORY.joinpath(".env")))

consul_templates.append(
    ConsulTemplateTemplate(
        source=str(
            consul_templates_directory.joinpath(".env_traefik_forward_auth.tmpl")
        ),
        destination=str(DOCKER_COMPOSE_DIRECTORY.joinpath(".env_traefik_forward_auth")),
    )
)
watched_docker_compose_files.append(
    str(DOCKER_COMPOSE_DIRECTORY.joinpath(".env_traefik_forward_auth"))
)

##################################################
# Configure Consul and Vault
consul_configuration = {
    Path("00-default.json"): ConsulConfig(
        addresses=ConsulAddresses(dns="127.0.0.1", http="127.0.0.1"),
        advertise_addr="{{ GetPrivateIP }}",
    )
}
consul = Consul(version=VERSIONS["consul"], configuration=consul_configuration)

consul_template = ConsulTemplate(
    version=VERSIONS["consul-template"],
    configuration={
        Path("00-default.json"): ConsulTemplateConfig(
            vault=ConsulTemplateVaultConfig(),
            template=consul_templates,
            restart_period="7d",
            restart_jitter="12h",
        )
    },
)
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
            config=VaultAutoAuthAWS(role="dagster-server"),
        ),
        sink=[VaultAutoAuthSink(type="file", config=[VaultAutoAuthFileSink()])],
    ),
    restart_period="5d",
    restart_jitter="12h",
)
vault = Vault(
    version=VERSIONS["vault"],
    configuration={Path("vault.json"): vault_config},
)

hashicorp_products = [vault, consul, consul_template]
for product in hashicorp_products:
    configure_hashicorp_product(product)

# Install and configure vector
vector_config = VectorConfig(is_docker=True, use_global_log_sink=True)
vector_config.configuration_templates[
    TEMPLATES_DIRECTORY.joinpath("vector", "dagster_logs.yaml")
] = {}
install_and_configure_vector(vector_config)

vault_template_permissions(vault_config)
consul_template_permissions(consul_template.configuration)

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
