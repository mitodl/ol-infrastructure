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
from bilder.facts.has_systemd import HasSystemd
from bilder.lib.linux_helpers import DOCKER_COMPOSE_DIRECTORY
from bridge.lib.magic_numbers import DEFAULT_HTTPS_PORT, VAULT_HTTP_PORT
from bridge.lib.versions import (
    CONSUL_TEMPLATE_VERSION,
    CONSUL_VERSION,
    DAGSTER_VERSION,
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
    "dagster": os.environ.get("DAGSTER_VERSION", DAGSTER_VERSION),
}

set_env_secrets(Path("consul/consul.env"))

server.user(
    name="Create the dagster user.",
    user="dagster",
    system=True,
    ensure_home=False,
)
watched_docker_compose_files = []

consul_templates_directory = Path("/etc/consul-template")
consul_templates = []

##################################################
# Put down EDX pipeline consul templates
edx_pipeline_files = [
    "micromasters.yaml",
    "mitx_bigquery.yaml",
    "mitxonline_edx.yaml",
    "open-discussions-enrollment-update.yaml",
    "open-discussions.yaml",
    "residential_edx.yaml",
    "xpro_edx.yaml",
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
# Place NGINX configuration items
nginx_conf_directory = Path("/etc/nginx")
nginx_conf_template_file = consul_templates_directory.joinpath(f"nginx.conf.tmpl")
nginx_conf_file = nginx_conf_directory.joinpath("nginx.conf")

nginx_htpasswd_file = nginx_conf_directory.joinpath("htpasswd")
nginx_htpasswd_template_file = consul_templates_directory.joinpath("htpasswd.tmpl")

certificate_file = nginx_conf_directory.joinpath("star.odl.mit.edu.crt")
certificate_key_file = nginx_conf_directory.joinpath("star.odl.mit.edu.key")

files.directory(
    name="Create NGINX directory",
    path=str(nginx_conf_directory),
    user="root",
    group="root",
    present=True,
)
files.put(
    name="Create the NGINX configuration consul-template file.",
    src=str(FILES_DIRECTORY.joinpath("nginx.conf.tmpl")),
    dest=str(nginx_conf_template_file),
    mode="0664",
)
consul_templates.append(
    ConsulTemplateTemplate(
        source=str(nginx_conf_template_file),
        destination=str(nginx_conf_file),
    )
)
watched_docker_compose_files.append(str(nginx_conf_file))


files.put(
    name="Create the NGINX htpasswd consul-template file.",
    src=str(FILES_DIRECTORY.joinpath("htpasswd.tmpl")),
    dest=str(nginx_htpasswd_template_file),
    mode="0664",
)
consul_templates.append(
    ConsulTemplateTemplate(
        source=str(nginx_htpasswd_template_file),
        destination=str(nginx_htpasswd_file),
    )
)
watched_docker_compose_files.append(str(nginx_htpasswd_file))

consul_templates.extend(
    [
        ConsulTemplateTemplate(
            contents='{{ with secret "secret-operations/global/odl_wildcard_cert" }}'
            "{{ printf .Data.key }}{{ end }}",
            destination=Path(certificate_key_file),
        ),
        ConsulTemplateTemplate(
            contents='{{ with secret "secret-operations/global/odl_wildcard_cert" }}'
            "{{ printf .Data.value }}{{ end }}",
            destination=Path(certificate_file),
        ),
    ]
)
watched_docker_compose_files.extend([str(certificate_file), str(certificate_key_file)])

##################################################
# Put down the docker compose configurations + .env
docker_compose_context = {
    "dagster_version": VERSIONS["dagster"],
    "edx_pipeline_definition_directory": edx_pipeline_directory,
    "listener_port": DEFAULT_HTTPS_PORT,
    "certificate_file": certificate_file,
    "certificate_key_file": certificate_key_file,
    "nginx_directory": nginx_conf_directory,
}
files.template(
    name="Place the dagster docker-compose.yaml file",
    src=str(TEMPLATES_DIRECTORY.joinpath("docker-compose.yaml.j2")),
    dest=str(Path(DOCKER_COMPOSE_DIRECTORY).joinpath("docker-compose.yaml")),
    context=docker_compose_context,
    mode="0664",
)
watched_docker_compose_files.append(
    str(Path(DOCKER_COMPOSE_DIRECTORY).joinpath("docker-compose.yaml"))
)

files.put(
    name="Place the dagster .env file.",
    src=str(FILES_DIRECTORY.joinpath(f".env.tmpl")),
    dest=str(consul_templates_directory.joinpath(f".env.tmpl")),
    mode="0664",
)
consul_templates.append(
    ConsulTemplateTemplate(
        source=str(consul_templates_directory.joinpath(f".env.tmpl")),
        destination=str(Path(DOCKER_COMPOSE_DIRECTORY).joinpath(".env")),
    )
)
watched_docker_compose_files.append(
    str(Path(DOCKER_COMPOSE_DIRECTORY).joinpath(".env"))
)

##################################################
# Configure Consul and Vault
consul_configuration = {
    Path("00-default.json"): ConsulConfig(
        addresses=ConsulAddresses(dns="127.0.0.1", http="127.0.0.1"),
        advertise_addr='{{ GetInterfaceIP "ens5" }}',
    )
}
consul = Consul(version=VERSIONS["consul"], configuration=consul_configuration)

consul_template = ConsulTemplate(
    version=VERSIONS["consul-template"],
    configuration={
        Path("00-default.json"): ConsulTemplateConfig(
            vault=ConsulTemplateVaultConfig(),
            template=consul_templates,
        )
    },
)
vault_config = VaultAgentConfig(
    cache=VaultAgentCache(use_auto_auth_token="force"),
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
    # template=vault_templates,
)
vault = Vault(
    version=VERSIONS["vault"],
    configuration={Path("vault.json"): vault_config},
)

hashicorp_products = [vault, consul, consul_template]
for product in hashicorp_products:
    configure_hashicorp_product(product)

vault_template_permissions(vault_config)
consul_template_permissions(consul_template.configuration)

vector_config = VectorConfig(is_proxy=False)

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
