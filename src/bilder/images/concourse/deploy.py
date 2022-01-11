import os
from functools import partial
from ipaddress import IPv4Address
from pathlib import Path
from typing import Union

from pydantic import SecretStr
from pyinfra import host
from pyinfra.api.util import get_template

from bilder.components.baseline.steps import (
    install_baseline_packages,
    service_configuration_watches,
)
from bilder.components.caddy.models import CaddyConfig
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
from bilder.components.hashicorp.consul.models import (
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
from bilder.facts import has_systemd  # noqa: F401
from bridge.lib.magic_numbers import (
    CONCOURSE_PROMETHEUS_EXPORTER_DEFAULT_PORT,
    CONCOURSE_WEB_HOST_COMMUNICATION_PORT,
    VAULT_HTTP_PORT,
)
from bridge.lib.versions import CONCOURSE_VERSION, CONSUL_VERSION, VAULT_VERSION

VERSIONS = {  # noqa: WPS407
    "concourse": os.environ.get("CONCOURSE_VERSION", CONCOURSE_VERSION),
    "consul": os.environ.get("CONSUL_VERSION", CONSUL_VERSION),
    "vault": os.environ.get("VAULT_VERSION", VAULT_VERSION),
}
CONCOURSE_WEB_NODE_TYPE = "web"
CONCOURSE_WORKER_NODE_TYPE = "worker"
node_type = host.data.node_type or os.environ.get("NODE_TYPE", CONCOURSE_WEB_NODE_TYPE)
# Set up configuration objects
concourse_base_config = ConcourseBaseConfig(version=VERSIONS["concourse"])
concourse_config_map = {
    CONCOURSE_WEB_NODE_TYPE: partial(  # noqa: S106
        ConcourseWebConfig,
        admin_user="oldevops",
        admin_password=(
            '{{ with secret "secret-concourse/web" }}'
            "{{ .Data.data.admin_password }}"
            "{{ end }}"
        ),
        database_user=(
            '{{ with secret "postgres-concourse/creds/app" }}'
            "{{ .Data.username }}"
            "{{ end }}"
        ),
        database_password=(
            '{{ with secret "postgres-concourse/creds/app" }}'
            "{{ .Data.password }}"
            "{{ end }}"
        ),
        enable_global_resources=True,
        public_domain=(
            '{{ with secret "secret-concourse/web" }}'
            "{{ .Data.data.public_domain }}"
            "{{ end }}"
        ),
        github_client_id=(
            '{{ with secret "secret-concourse/web" }}'
            "{{ .Data.data.github_client_id }}"
            "{{ end }}"
        ),
        github_client_secret=(
            '{{ with secret "secret-concourse/web" }}'
            "{{ .Data.data.github_client_secret }}"
            "{{ end }}"
        ),
        enable_p2p_volume_streaming=True,
        p2p_volume_streaming_timeout="10m",
        prometheus_bind_ip=IPv4Address("127.0.0.1"),
        prometheus_bind_port=CONCOURSE_PROMETHEUS_EXPORTER_DEFAULT_PORT,
        secret_cache_duration="5m",
        secret_cache_enabled=True,
        vault_url=f"http://localhost:{VAULT_HTTP_PORT}",
        vault_client_token="this-token-gets-overridden-by-the-vault-agent",
        vault_path_prefix="/secret-concourse",
        vault_insecure_skip_verify=True,
    ),
    CONCOURSE_WORKER_NODE_TYPE: partial(
        ConcourseWorkerConfig,
        baggageclaim_driver="overlay",
        container_runtime="containerd",
        containerd_dns_server="8.8.8.8",
        containerd_max_containers=0,  # Don't set a limit on the number of containers
        containerd_network_pool="10.250.0.0/16",
        baggageclaim_p2p_interface_family="4"
        baggageclaim_p2p_interface_name_pattern="ens\d+"
    ),
}
concourse_config: Union[
    ConcourseWebConfig, ConcourseWorkerConfig
] = concourse_config_map[
    node_type
]()  # type: ignore
vault_template_map = {
    CONCOURSE_WEB_NODE_TYPE: [
        partial(
            VaultTemplate,
            contents=(
                '{{ with secret "secret-concourse/web" }}'
                "{{ printf .Data.data.tsa_private_key }}{{ end }}"
            ),
            destination=concourse_config.dict().get("tsa_host_key_path"),
        ),
        partial(
            VaultTemplate,
            contents=(
                '{{ with secret "secret-concourse/web" }}'
                "{{ printf .Data.data.session_signing_key }}{{ end }}"
            ),
            destination=concourse_config.dict().get("session_signing_key_path"),
        ),
        partial(
            VaultTemplate,
            contents=(
                '{{ with secret "secret-concourse/web" }}'
                "{{ printf .Data.data.worker_public_key }}{{ end }}"
            ),
            destination=concourse_config.dict().get("authorized_keys_file"),
        ),
        partial(
            VaultTemplate,
            contents=(
                '{{ with secret "secret-operations/global/odl_wildcard_cert" }}'
                "{{ printf .Data.value }}{{ end }}"
            ),
            destination=Path("/etc/caddy/odl_wildcard.cert"),
        ),
        partial(
            VaultTemplate,
            contents=(
                '{{ with secret "secret-operations/global/odl_wildcard_cert" }}'
                "{{ printf .Data.key }}{{ end }}"
            ),
            destination=Path("/etc/caddy/odl_wildcard.key"),
        ),
    ],
    CONCOURSE_WORKER_NODE_TYPE: [
        partial(
            VaultTemplate,
            contents=(
                '{{ with secret "secret-concourse/worker" }}'
                "{{ printf .Data.data.worker_private_key }}{{ end }}"
            ),
            destination=concourse_config.dict().get("worker_private_key_path"),
        ),
        partial(
            VaultTemplate,
            contents=(
                '{{ with secret "secret-concourse/worker" }}'
                "{{ printf .Data.data.tsa_public_key }}{{ end }}"
            ),
            destination=concourse_config.dict().get("tsa_public_key_path"),
        ),
    ],
}

# Install Concourse
install_baseline_packages(packages=["curl", "btrfs-progs"])
concourse_install_changed = install_concourse(concourse_base_config)
concourse_config_changed = configure_concourse(concourse_config)

consul_configuration = {Path("00-default.json"): ConsulConfig()}

if concourse_config._node_type == CONCOURSE_WEB_NODE_TYPE:  # noqa: WPS437
    # Setting this attribute after instantiating the object to bypass validation
    concourse_config.encryption_key = SecretStr(
        '{{ with secret "secret-concourse/web" }}'
        "{{ .Data.data.encryption_key }}"
        "{{ end }}"
    )
    consul_configuration[Path("01-concourse.json")] = ConsulConfig(
        services=[
            ConsulService(
                name="concourse",
                port=CONCOURSE_WEB_HOST_COMMUNICATION_PORT,
                tags=[CONCOURSE_WEB_NODE_TYPE],
                check=ConsulServiceTCPCheck(
                    name="concourse-web-job-queue",
                    tcp="localhost:8080",
                    interval="10s",
                ),
            )
        ]
    )

    # Install Caddy
    caddy_config = CaddyConfig(
        caddyfile=Path(__file__)
        .resolve()
        .parent.joinpath("templates", "caddyfile.j2"),
    )
    caddy_config.template_context = caddy_config.dict()
    install_caddy(caddy_config)
    caddy_config_changed = configure_caddy(caddy_config)
    if host.fact.has_systemd:
        service_configuration_watches(
            service_name="caddy",
            watched_files=[
                Path("/etc/caddy/odl_wildcard.cert"),
                Path("/etc/caddy/odl_wildcard.key"),
            ],
        )
        caddy_service(caddy_config=caddy_config, do_reload=caddy_config_changed)
    vector_config = VectorConfig(
        configuration_templates={
            Path(__file__)
            .resolve()
            .parent.joinpath("templates", "vector.yaml"): {
                "concourse_prometheus_port": concourse_config.prometheus_bind_port
            },
        },
    )
    install_vector(vector_config)
    configure_vector(vector_config)
    vector_service(vector_config)

# Install Consul and Vault Agent
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
        address=f"https://active.vault.service.consul:{VAULT_HTTP_PORT}",
        tls_skip_verify=True,
    ),
    auto_auth=VaultAutoAuthConfig(
        method=VaultAutoAuthMethod(
            type="aws",
            mount_path="auth/aws",
            config=VaultAutoAuthAWS(
                role=f"concourse-{concourse_config._node_type}"  # noqa: WPS437
            ),
        ),
        sink=[VaultAutoAuthSink(type="file", config=[VaultAutoAuthFileSink()])],
    ),
    template=[partial_func() for partial_func in vault_template_map[node_type]]
    + [
        VaultTemplate(
            contents=get_template(
                "{% for env_var, env_value in concourse_env.items() -%}\n"
                "{{ env_var }}={{ env_value }}\n{% endfor -%}",
                is_string=True,
            ).render(concourse_env=concourse_config.concourse_env()),
            destination=concourse_base_config.env_file_path,
        ),
    ],
)
vault = Vault(
    version=VERSIONS["vault"],
    configuration={Path("vault.json"): vault_config},
)
consul = Consul(version=VERSIONS["consul"], configuration=consul_configuration)
hashicorp_products = [vault, consul]
install_hashicorp_products(hashicorp_products)
vault_template_permissions(vault_config)
for product in hashicorp_products:
    configure_hashicorp_product(product)

# Manage services
if host.fact.has_systemd:
    register_concourse_service(
        concourse_config, restart=concourse_install_changed or concourse_config_changed
    )
    register_services(hashicorp_products, start_services_immediately=False)
    proxy_consul_dns()
    watched_concourse_files = [
        concourse_config.env_file_path,
    ]
    if node_type == CONCOURSE_WEB_NODE_TYPE:
        watched_concourse_files.extend(
            [
                concourse_config.authorized_keys_file,
                concourse_config.session_signing_key_path,
                concourse_config.tsa_host_key_path,
            ]
        )
    else:
        watched_concourse_files.extend(
            [
                concourse_config.worker_private_key_path,
                concourse_config.tsa_public_key_path,
            ]
        )
    service_configuration_watches(
        service_name="concourse",
        watched_files=watched_concourse_files,
    )
