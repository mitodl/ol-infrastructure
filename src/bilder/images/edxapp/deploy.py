import os
import tempfile
from pathlib import Path

from pyinfra import host
from pyinfra.operations import files, pip

from bilder.components.baseline.steps import service_configuration_watches
from bilder.components.hashicorp.consul.models.consul import (
    Consul,
    ConsulConfig,
    ConsulService,
    ConsulServiceTCPCheck,
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
    VaultTemplate,
)
from bilder.components.hashicorp.vault.steps import vault_template_permissions
from bilder.facts import has_systemd  # noqa: F401
from bridge.lib.magic_numbers import VAULT_HTTP_PORT

VERSIONS = {  # noqa: WPS407
    "consul": "1.10.0",
    "vault": "1.7.3",
}

WEB_NODE_TYPE = "web"
WORKER_NODE_TYPE = "worker"
node_type = host.data.node_type or os.environ.get("NODE_TYPE", WEB_NODE_TYPE)

# Install additional Python dependencies
pip.packages(
    name="Install additional edX dependencies",
    packages=[
        "redis-py-cluster",  # Support for clustered redis
        "django-redis",  # Support for Redis caching in Django
        "celery-redbeat",  # Support for using Redis as the lock for Celery schedules
        "mitxpro-openedx-extensions==0.2.2",
        "social-auth-mitxpro==0.4",
        "edx-username-changer==0.2.0",
    ],
    present=True,
    virtualenv="/edx/app/edxapp/venvs/edxapp/",
    sudo_user="edxapp",
)

consul_configuration = {Path("00-default.json"): ConsulConfig()}

if node_type == WEB_NODE_TYPE:
    consul_configuration[Path("01-edxapp.json")] = ConsulConfig(
        services=[
            ConsulService(
                name="edxapp",
                port=8080,  # noqa: WPS432
                tags=["lms"],
                check=ConsulServiceTCPCheck(
                    name="edxapp-lms",
                    tcp="localhost:8080",
                    interval="10s",
                ),
            )
        ]
    )

studio_template_path = Path("/etc/vault/templates/edxapp_studio.yml.tmpl")
lms_template_path = Path("/etc/vault/templates/edxapp_lms.yml.tmpl")
lms_config_path = Path("/edx/etc/lms.yml")
studio_config_path = Path("/edx/etc/studio.yml")
# Install Consul and Vault Agent
vault = Vault(
    version=VERSIONS["vault"],
    configuration=VaultAgentConfig(
        cache=VaultAgentCache(use_auto_auth_token="force"),  # noqa: S106
        listener=[
            VaultListener(
                type="tcp", address=f"127.0.0.1:{VAULT_HTTP_PORT}", tls_disable=True
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
                config=VaultAutoAuthAWS(role=f"edxapp-{node_type}"),
            ),
            sink=[VaultAutoAuthSink(type="file", config=[VaultAutoAuthFileSink()])],
        ),
        template=[
            VaultTemplate(
                source=lms_template_path,
                destination=lms_config_path,
            ),
            VaultTemplate(
                source=studio_template_path,
                destination=studio_config_path,
            ),
        ],
    ),
)
consul = Consul(version=VERSIONS["consul"], configuration=consul_configuration)
hashicorp_products = [vault, consul]
install_hashicorp_products(hashicorp_products)
vault_template_permissions(vault.configuration)
for product in hashicorp_products:
    configure_hashicorp_product(product)

# Upload templates for Vault agent
common_config = Path(__file__).parent.joinpath("templates", "common_values.yml")
studio_config = Path(__file__).parent.joinpath("templates", "studio_only.yml")
lms_config = Path(__file__).parent.joinpath("templates", "lms_only.yml")
with tempfile.NamedTemporaryFile("wt", delete=False) as studio_template:
    studio_template.write(common_config.read_text())
    studio_template.write(studio_config.read_text())
    files.put(
        name="Upload studio.yml template for Vault agent",
        src=studio_template.name,
        dest=studio_template_path,
        user=vault.name,
        group=vault.name,
        create_remote_dir=True,
    )
with tempfile.NamedTemporaryFile("wt", delete=False) as lms_template:
    lms_template.write(common_config.read_text())
    lms_template.write(lms_config.read_text())
    files.put(
        name="Upload lms.yml template for Vault agent",
        src=lms_template.name,
        dest=lms_template_path,
        user=vault.name,
        group=vault.name,
        create_remote_dir=True,
    )
# Manage services
if host.fact.has_systemd:
    register_services(hashicorp_products, start_services_immediately=False)
    service_configuration_watches(
        service_name="edxapp-lms",
        watched_files=[lms_config_path],
        onchange_command=(
            f"chown www-data:edxapp {lms_config_path} &&"
            " /edx/bin/supervisorctl restart lms"
        ),
    )
    service_configuration_watches(
        service_name="edxapp-cms",
        watched_files=[studio_config_path],
        onchange_command=(
            f"chown www-data:edxapp {studio_config_path} &&"
            " /edx/bin/supervisorctl restart cms"
        ),
    )
    proxy_consul_dns()
