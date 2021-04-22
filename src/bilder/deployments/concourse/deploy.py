import os
from functools import partial
from pathlib import Path

from pydantic import SecretStr
from pyinfra import host
from pyinfra.api.util import get_template

from bilder.components.baseline.setup import install_baseline_packages
from bilder.components.caddy.models import CaddyConfig, CaddyPlugin
from bilder.components.caddy.steps import caddy_service, configure_caddy, install_caddy
from bilder.components.concourse.models import (
    ConcourseBaseConfig,
    ConcourseWebConfig,
    ConcourseWorkerConfig,
)
from bilder.components.concourse.steps import (
    configure_concourse,
    install_concourse,
    register_concourse_service,
)
from bilder.components.hashicorp.consul.models.consul import Consul, ConsulConfig
from bilder.components.hashicorp.steps import (
    configure_hashicorp_product,
    install_hashicorp_products,
    register_services,
)
from bilder.components.hashicorp.vault.models import (
    Vault,
    VaultAgentConfig,
    VaultAutoAuthAWS,
    VaultAutoAuthConfig,
    VaultAutoAuthFileSink,
    VaultAutoAuthMethod,
    VaultAutoAuthSink,
    VaultTemplate,
)
from bilder.facts import has_systemd  # noqa: F401

node_type = host.data.node_type or os.environ.get("NODE_TYPE", "web")
# Set up configuration objects
concourse_base_config = ConcourseBaseConfig(version="7.1.0")
concourse_config_map = {
    "web": partial(  # noqa: S106
        ConcourseWebConfig,
        admin_user="oldevops",
        admin_password=(
            "{{ with secret 'secret-concourse/web' }}"
            "{{ .Data.data.admin_password }}"
            "{{ end }}"
        ),
        database_user=(
            "{{ with secret 'postgres-concourse/creds/concourse' }}"
            "{{ .Data.username }}"
            "{{ end }}"
        ),
        database_password=(
            "{{ with secret 'postgres-concourse/creds/concourse' }}"
            "{{ .Data.password }}"
            "{{ end }}"
        ),
    ),
    "worker": partial(ConcourseWorkerConfig),
}
concourse_config = concourse_config_map[node_type]()
if concourse_config._node_type == "web":  # noqa: WPS437
    # Setting this attribute after instantiating the object to bypass validation
    concourse_config.encryption_key = SecretStr(
        "{{ with secret 'secret-concourse/web' }}"
        "{{ .Data.data.encryption_key }}"
        "{{ end }}"
    )
vault_template_map = {
    "web": [
        partial(
            VaultTemplate,
            contents=(
                "{{ with secret 'secret-concourse/tsa_key' }}"
                "{{ .Data.private_key }}{{ end }}"
            ),
            destination=concourse_config.dict().get("tsa_host_key_path"),
        ),
        partial(
            VaultTemplate,
            contents=(
                "{{ with secret 'secret-concourse/web' }}"
                "{{ .Data.data.session_signing_key }}{{ end }}"
            ),
            destination=concourse_config.dict().get("session_signing_key_path"),
        ),
    ],
    "worker": [
        partial(
            VaultTemplate,
            contents=(
                "{{ with secret 'secret-concourse/generic_worker_key' }}"
                "{{ .Data.private_key }}{{ end }}"
            ),
            destination=concourse_config.dict().get("worker_private_key_path"),
        ),
        partial(
            VaultTemplate,
            contents=(
                "{{ with secret 'secret-concourse/tsa_key' }}"
                "{{ .Data.public_key }}{{ end }}"
            ),
            destination=concourse_config.dict().get("tsa_public_key_path"),
        ),
    ],
}

# Install Concourse
install_baseline_packages()
install_changed = install_concourse(concourse_base_config)
config_changed = configure_concourse(concourse_config)

# Install Consul and Vault Agent
hashicorp_products = [
    Consul(configuration={Path("/etc/consul.d/00-default.json"): ConsulConfig()}),
    Vault(
        configuration=VaultAgentConfig(
            auto_auth=VaultAutoAuthConfig(
                method=VaultAutoAuthMethod(
                    type="aws",
                    mount_path="auth/aws",
                    config=VaultAutoAuthAWS(
                        role=f"concourse-{concourse_config._node_type}"  # noqa: WPS437
                    ),
                ),
                sinks=[VaultAutoAuthSink(type="file", config=VaultAutoAuthFileSink())],
            ),
            template=[partial_func() for partial_func in vault_template_map[node_type]]
            + [
                VaultTemplate(
                    contents=get_template(
                        "{% for env_var, env_value in concourse_env.items() -%}\n"
                        "{{ env_var }}={{ env_value }}\n {% endfor -%}",
                        is_string=True,
                    ).render(concourse_env=concourse_config.concourse_env()),
                    destination=concourse_base_config.env_file_path,
                ),
            ],
        )
    ),
]
install_hashicorp_products(hashicorp_products)

# Install Caddy
caddy_config = CaddyConfig(
    caddyfile=Path(__file__)
    .parent.resolve()
    .joinpath("templates", "concourse_caddyfile.j2"),
    plugins=[CaddyPlugin(repository="github.com/caddy-dns/route53", version="v1.1.1")],
)
caddy_config.template_context = caddy_config.dict()
install_caddy(caddy_config)
caddy_config_changed = configure_caddy(caddy_config)

# Manage services
if host.fact.has_systemd:
    register_concourse_service(
        concourse_config, restart=install_changed or config_changed
    )
    caddy_service(do_reload=caddy_config_changed)
    register_services(hashicorp_products)

for product in hashicorp_products:
    configure_hashicorp_product(product)
