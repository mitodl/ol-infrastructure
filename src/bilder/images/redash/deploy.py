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
from bilder.facts.has_systemd import HasSystemd
from bilder.lib.linux_helpers import DOCKER_COMPOSE_DIRECTORY
from bridge.lib.magic_numbers import (
    DEFAULT_HTTP_PORT,
    DEFAULT_HTTPS_PORT,
    VAULT_HTTP_PORT,
)
from bridge.lib.versions import (
    CONSUL_TEMPLATE_VERSION,
    CONSUL_VERSION,
    REDASH_VERSION,
    VAULT_VERSION,
)
from bridge.secrets.sops import set_env_secrets


def place_jinja_template_file(
    name: str,
    repo_path: Path,
    destination_path: Path,
    context: dict,
    watched_files: list[Path],
    mode: str = "0644",
):
    files.template(
        name=f"Place and interpolate {name} jinja template file",
        src=str(repo_path.joinpath(name + ".j2")),
        dest=str(destination_path.joinpath(name)),
        context=context,
        mode="0664",
    )
    watched_files.append(destination_path.joinpath(name))


def place_consul_template_file(
    name: str,
    repo_path: Path,
    template_path: Path,
    destination_path: Path,
    consul_templates: list[ConsulTemplateTemplate],
    watched_files: list[Path],
    mode: str = "0664",
):
    files.put(
        name=f"Place {name} template file.",
        src=str(repo_path.joinpath(name + ".tmpl")),
        dest=str(template_path.joinpath(name + ".tmpl")),
        mode=mode,
    )
    consul_templates.append(
        ConsulTemplateTemplate(
            source=template_path.joinpath(name + ".tmpl"),
            destination=destination_path.joinpath(name),
        )
    )
    watched_files.append(destination_path.joinpath(name))


TEMPLATES_DIRECTORY = Path(__file__).resolve().parent.joinpath("templates")
FILES_DIRECTORY = Path(__file__).resolve().parent.joinpath("files")
VERSIONS = {
    "consul": os.environ.get("CONSUL_VERSION", CONSUL_VERSION),
    "consul-template": os.environ.get(
        "CONSUL_TEMPLATE_VERSION", CONSUL_TEMPLATE_VERSION
    ),
    "vault": os.environ.get("VAULT_VERSION", VAULT_VERSION),
    "redash": os.environ.get("REDASH_VERSION", REDASH_VERSION),
}

set_env_secrets(Path("consul/consul.env"))

files.put(
    name="Set the redash version",
    src=io.StringIO(VERSIONS["redash"]),
    dest="/etc/defaults/consul-template",
)
consul_templates: list[ConsulTemplateTemplate] = []
watched_files: list[Path] = []

nginx_conf_directory = Path("/etc/nginx")
shib_conf_directory = Path("/etc/shibboleth")

certificate_file = nginx_conf_directory.joinpath("star.odl.mit.edu.crt")
certificate_key_file = nginx_conf_directory.joinpath("star.odl.mit.edu.key")

sp_signing_cert_file = shib_conf_directory.joinpath("sp-signing-cert.pem")
sp_signing_key_file = shib_conf_directory.joinpath("sp-signing-key.pem")
sp_encrypting_cert_file = shib_conf_directory.joinpath("sp-encrypting-cert.pem")
sp_encrypting_key_file = shib_conf_directory.joinpath("sp-encrypting-key.pem")

# Zeroly Create needed etc directories
files.directory(
    name="Create NGINX directory",
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

# Firstly place down normal files requiring no templating
# Assumes no file special file extension
untemplated_files = {
    "shib_fastcgi_params": nginx_conf_directory,
    "fastcgi_params": nginx_conf_directory,
    "fastcgi.conf": nginx_conf_directory,
    "shib_clear_headers": nginx_conf_directory,
    "attribute-map.xml": shib_conf_directory,
    "mit-md-cert.pem": shib_conf_directory,
}
for filename, dest_dir in untemplated_files.items():
    files.put(
        name=f"Place {filename} file.",
        src=str(FILES_DIRECTORY.joinpath(filename)),
        dest=str(dest_dir.joinpath(filename)),
        mode="0664",
    )

# Secondly place down jinja2 templates rendered at AMI bake time
# assumes .j2 file extension that will be stripped
jinja_templated_files = {
    "docker-compose.yaml": Path(DOCKER_COMPOSE_DIRECTORY),
}
jinja_context = {
    "redash_version": VERSIONS["redash"],
    "web_worker_count": 4,
    "rq_worker_count": 1,
    "scheduled_worker_count": 1,
    "adhoc_worker_count": 1,
    "unsecure_listener_port": DEFAULT_HTTP_PORT,
    "listener_port": DEFAULT_HTTPS_PORT,
    "certificate_file": certificate_file,
    "certificate_key_file": certificate_key_file,
    "nginx_directory": nginx_conf_directory,
    "shib_directory": shib_conf_directory,
}
for filename, dest_dir in jinja_templated_files.items():
    place_jinja_template_file(
        name=filename,
        repo_path=TEMPLATES_DIRECTORY,
        destination_path=dest_dir,
        context=jinja_context,
        watched_files=watched_files,
    )

# Thirdly, place down consul-template files
# assume .tmpl file extension that will be retained
consul_templated_files = {
    "nginx.conf": nginx_conf_directory,
    ".env": Path(DOCKER_COMPOSE_DIRECTORY),
    "shibboleth2.xml": shib_conf_directory,
}
for filename, dest_dir in consul_templated_files.items():
    place_consul_template_file(
        name=filename,
        repo_path=FILES_DIRECTORY,
        template_path=Path("/etc/consul-template"),
        destination_path=dest_dir,
        consul_templates=consul_templates,
        watched_files=watched_files,
    )

# Add consul template confgs for files that are just populated straight from vault.
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
        ConsulTemplateTemplate(
            contents='{{ with secret "secret-data/redash/sp-certificate-data" }}'
            "{{ printf .Data.sp_signing_cert }}{{ end }}",
            destination=Path(sp_signing_cert_file),
        ),
        ConsulTemplateTemplate(
            contents='{{ with secret "secret-data/redash/sp-certificate-data" }}'
            "{{ printf .Data.sp_signing_key }}{{ end }}",
            destination=Path(sp_signing_key_file),
        ),
        ConsulTemplateTemplate(
            contents='{{ with secret "secret-data/redash/sp-certificate-data" }}'
            "{{ printf .Data.sp_encrypting_cert }}{{ end }}",
            destination=Path(sp_encrypting_cert_file),
        ),
        ConsulTemplateTemplate(
            contents='{{ with secret "secret-data/redash/sp-certificate-data" }}'
            "{{ printf .Data.sp_encrypting_key }}{{ end }}",
            destination=Path(sp_encrypting_key_file),
        ),
    ]
)
watched_files.extend(
    [
        certificate_key_file,
        certificate_file,
        sp_signing_cert_file,
        sp_signing_key_file,
        sp_encrypting_cert_file,
        sp_encrypting_key_file,
    ]
)

# Install and configure vector ???
# TODO
vector_config = VectorConfig(is_proxy=False)

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
            config=VaultAutoAuthAWS(role="redash-server"),
        ),
        sink=[VaultAutoAuthSink(type="file", config=[VaultAutoAuthFileSink()])],
    ),
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
        )
    },
)

# Consul and vault were installed in the docker-baseline-ami, consul-template is missing
install_hashicorp_products([consul_template])

hashicorp_products = [vault, consul, consul_template]

for product in hashicorp_products:
    configure_hashicorp_product(product)

consul_template_permissions(consul_template.configuration)

# Finally, setup the system configurations and file watches.
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
        service_name="docker-compose",
        watched_files=watched_files,
    )
