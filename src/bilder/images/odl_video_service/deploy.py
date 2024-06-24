import io
import os
from pathlib import Path

from bridge.lib.magic_numbers import VAULT_HTTP_PORT
from bridge.lib.versions import (
    CONSUL_TEMPLATE_VERSION,
    CONSUL_VERSION,
    OVS_VERSION,
    VAULT_VERSION,
)
from bridge.secrets.sops import set_env_secrets
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
)
from bilder.components.vector.models import VectorConfig
from bilder.components.vector.steps import install_and_configure_vector
from bilder.facts.has_systemd import HasSystemd
from bilder.lib.linux_helpers import DOCKER_COMPOSE_DIRECTORY
from bilder.lib.template_helpers import (
    CONSUL_TEMPLATE_DIRECTORY,
    place_consul_template_file,
)

TEMPLATES_DIRECTORY = Path(__file__).resolve().parent.joinpath("templates")
FILES_DIRECTORY = Path(__file__).resolve().parent.joinpath("files")
VERSIONS = {
    "consul": os.environ.get("CONSUL_VERSION", CONSUL_VERSION),
    "consul-template": os.environ.get(
        "CONSUL_TEMPLATE_VERSION", CONSUL_TEMPLATE_VERSION
    ),
    "vault": os.environ.get("VAULT_VERSION", VAULT_VERSION),
    "ovs": os.environ.get("OVS_VERSION", OVS_VERSION),
}

set_env_secrets(Path("consul/consul.env"))

files.put(
    name=f"Set the odl-video-service version to {VERSIONS['ovs']}",
    src=io.StringIO(VERSIONS["ovs"]),
    dest="/etc/default/ovs-version",
)

files.directory(
    name="Create docker compose directory",
    path=str(DOCKER_COMPOSE_DIRECTORY),
    user="root",
    group="root",
    present=True,
)

files.directory(
    name="Create staticfiles directory",
    path=str(DOCKER_COMPOSE_DIRECTORY.joinpath("staticfiles")),
    user="1000",
    group="1000",
    present=True,
)

files.directory(
    name="Create /var/log/odl-video directory",
    path="/var/log/odl-video",
    user="root",
    group="root",
    mode="777",
)

nginx_conf_directory = Path("/etc/nginx")
shib_conf_directory = Path("/etc/shibboleth")

files.directory(
    name="Create NGINX directory.",
    path=str(nginx_conf_directory),
    user="root",
    group="root",
    present=True,
)
files.directory(
    name="Create Shibboleth directory",
    path=str(shib_conf_directory),
    user="root",
    group="root",
    present=True,
)

watched_files: list[Path] = []
consul_templates: list[ConsulTemplateTemplate] = []

# Firstly play down normal files requiring no special templating
untemplated_files = {
    "fastcgi_params": nginx_conf_directory,
    "logging.conf": nginx_conf_directory,
    "shib_clear_headers": nginx_conf_directory,
    "shib_fastcgi_params": nginx_conf_directory,
    "shib_params": nginx_conf_directory,
    "uwsgi_params": nginx_conf_directory,
    "attribute-map.xml": shib_conf_directory,
    #    "mit-md-cert.pem": shib_conf_directory,
}
for ut_filename, dest_dir in untemplated_files.items():
    files.put(
        name=f"Place {ut_filename} file.",
        src=str(FILES_DIRECTORY.joinpath(ut_filename)),
        dest=str(dest_dir.joinpath(ut_filename)),
        mode="0664",
    )
    watched_files.append(dest_dir.joinpath(ut_filename))

# Place down consul-template files
# Assume a .tmpl file extension that will be retained
consul_templated_files = {
    "nginx_with_shib.conf": (nginx_conf_directory, "0660"),
    "nginx_wo_shib.conf": (nginx_conf_directory, "0660"),
    ".env": (DOCKER_COMPOSE_DIRECTORY, "0660"),
    "docker-compose.yaml": (DOCKER_COMPOSE_DIRECTORY, "0660"),
    "shibboleth2.xml": (shib_conf_directory, "0664"),
}
for ct_filename, dest_tuple in consul_templated_files.items():
    template = place_consul_template_file(
        name=ct_filename,
        repo_path=FILES_DIRECTORY,
        template_path=Path(CONSUL_TEMPLATE_DIRECTORY),
        destination_path=dest_tuple[0],
        mode=dest_tuple[1],
    )
    consul_templates.append(template)
    watched_files.append(template.destination)

# Create a few in-line consul templates for the wildcard certificate + key
certificate_file = nginx_conf_directory.joinpath("star.odl.mit.edu.crt")
certificate_key_file = nginx_conf_directory.joinpath("star.odl.mit.edu.key")
sp_certificate_file = shib_conf_directory.joinpath("sp-cert.pem")
sp_key_file = shib_conf_directory.joinpath("sp-key.pem")
mit_md_certificate_file = shib_conf_directory.joinpath("mit-md-cert.pem")

consul_templates.extend(
    [
        ConsulTemplateTemplate(
            contents=(
                '{{ with secret "secret-odl-video-service/ovs-secrets" }}'
                "{{ printf .Data.data.nginx.tls_key }}{{ end }}"
            ),
            destination=Path(certificate_key_file),
        ),
        ConsulTemplateTemplate(
            contents=(
                '{{ with secret "secret-odl-video-service/ovs-secrets" }}'
                "{{ printf .Data.data.nginx.tls_certificate }}{{ end }}"
            ),
            destination=Path(certificate_file),
        ),
        ConsulTemplateTemplate(
            contents=(
                '{{ with secret "secret-odl-video-service/ovs-secrets" }}'
                "{{ printf .Data.data.shibboleth.sp_cert }}{{ end }}"
            ),
            destination=Path(sp_certificate_file),
        ),
        ConsulTemplateTemplate(
            contents=(
                '{{ with secret "secret-odl-video-service/ovs-secrets" }}'
                "{{ printf .Data.data.shibboleth.sp_key }}{{ end }}"
            ),
            destination=Path(sp_key_file),
        ),
        ConsulTemplateTemplate(
            contents=(
                '{{ with secret "secret-odl-video-service/ovs-secrets" }}'
                "{{ printf .Data.data.shibboleth.mit_md_cert }}{{ end }}"
            ),
            destination=Path(mit_md_certificate_file),
        ),
    ]
)
watched_files.extend([certificate_file, certificate_key_file])

# Install and Configure Consul and Vault
consul_configuration = {
    Path("00-default.json"): ConsulConfig(
        addresses=ConsulAddresses(dns="127.0.0.1", http="127.0.0.1"),
        advertise_addr='{{ GetInterfaceIP "ens5" }}',
        services=[],
    )
}

# Install vault, consul, and consul-template
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
            config=VaultAutoAuthAWS(role="ovs-server"),
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

# Install and configure vector
vector_config = VectorConfig(is_docker=True, use_global_log_sink=True)
vector_config.configuration_templates[
    TEMPLATES_DIRECTORY.joinpath("vector", "odl_video_service_logs.yaml")
] = {}
install_and_configure_vector(vector_config)

# Install consul-template because the docker-baseline-ami doesn't come with it
install_hashicorp_products([consul_template])

hashicorp_products = [vault, consul, consul_template]
for product in hashicorp_products:
    configure_hashicorp_product(product)

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
        service_name="docker-compose", watched_files=watched_files
    )
