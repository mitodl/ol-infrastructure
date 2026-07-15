import io
import os
from functools import partial
from ipaddress import IPv4Address
from pathlib import Path

import yaml
from pyinfra import host
from pyinfra.api.util import get_template
from pyinfra.operations import files, systemd

from bilder.components.alloy.models import AlloyConfig
from bilder.components.alloy.steps import install_and_configure_alloy
from bilder.components.baseline.steps import (
    install_baseline_packages,
    service_configuration_watches,
)
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
from bilder.components.traefik.models import traefik_file_provider, traefik_static
from bilder.components.traefik.models.component import TraefikConfig
from bilder.components.traefik.steps import (
    configure_traefik,
    install_traefik_binary,
    traefik_service,
)
from bilder.components.vector.models import VectorConfig
from bilder.components.vector.steps import (
    install_and_configure_vector,
)
from bilder.facts.has_systemd import HasSystemd
from bridge.lib.magic_numbers import (
    CONCOURSE_PROMETHEUS_EXPORTER_DEFAULT_PORT,
    CONCOURSE_WEB_HOST_COMMUNICATION_PORT,
    VAULT_HTTP_PORT,
)
from bridge.lib.versions import (
    CONCOURSE_VERSION,
    CONSUL_VERSION,
    TRAEFIK_VERSION,
    VAULT_VERSION,
)
from bridge.secrets.sops import set_env_secrets

VERSIONS = {
    "concourse": os.environ.get("CONCOURSE_VERSION", CONCOURSE_VERSION),
    "consul": os.environ.get("CONSUL_VERSION", CONSUL_VERSION),
    "vault": os.environ.get("VAULT_VERSION", VAULT_VERSION),
    "traefik": os.environ.get("TRAEFIK_VERSION", TRAEFIK_VERSION),
}
FILES_DIRECTORY = Path(__file__).parent.joinpath("files")
TEMPLATES_DIRECTORY = Path(__file__).parent.joinpath("templates")
CONCOURSE_WEB_NODE_TYPE = "web"
CONCOURSE_WORKER_NODE_TYPE = "worker"
node_type = os.environ.get("NODE_TYPE", CONCOURSE_WEB_NODE_TYPE)
# Set up configuration objects
set_env_secrets(Path("consul/consul.env"))
concourse_base_config = ConcourseBaseConfig(version=VERSIONS["concourse"])
concourse_config_map = {
    CONCOURSE_WEB_NODE_TYPE: partial(
        ConcourseWebConfig,
        admin_user="oldevops",
        admin_password=(  # noqa: S106
            '{{ with secret "secret-concourse/web" }}'
            "{{ .Data.data.admin_password }}"
            "{{ end }}"
        ),
        ocw_user_password=(  # noqa: S106
            '{{ with secret "secret-concourse/web" }}'
            "{{ .Data.data.ocw_user_password }}"
            "{{ end }}"
        ),
        release_bot_password=(  # noqa: S106
            '{{ with secret "secret-concourse/web" }}'
            "{{ .Data.data.release_bot_password }}"
            "{{ end }}"
        ),
        container_placement_strategy="fewest-build-containers,volume-locality",
        database_user=(
            '{{ with secret "postgres-concourse/creds/app" }}'
            "{{ .Data.username }}"
            "{{ end }}"
        ),
        database_password=(  # noqa: S106
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
        github_client_secret=(  # noqa: S106
            '{{ with secret "secret-concourse/web" }}'
            "{{ .Data.data.github_client_secret }}"
            "{{ end }}"
        ),
        oidc_display_name="OL Platform Engineering",
        oidc_client_id=(
            '{{ with secret "secret-operations/sso/concourse" }}'
            "{{ .Data.client_id }}"
            "{{ end }}"
        ),
        oidc_client_secret=(  # noqa: S106
            '{{ with secret "secret-operations/sso/concourse" }}'
            "{{ .Data.client_secret }}"
            "{{ end }}"
        ),
        oidc_issuer=(
            '{{ with secret "secret-operations/sso/concourse" }}'
            "{{ .Data.url }}"
            "{{ end }}"
        ),
        oidc_user_name_key="preferred_username",
        main_team_config="/etc/concourse/teams/main.yaml",
        default_build_logs_to_retain="10",
        default_days_to_retain_build_logs="10",
        enable_build_auditing=False,
        enable_container_auditing=False,
        enable_job_auditing=False,
        enable_pipeline_auditing=False,
        enable_resource_auditing=False,
        enable_rerun_when_worker_disappears=True,
        enable_system_auditing=False,
        enable_team_auditing=False,
        enable_volume_auditing=False,
        enable_worker_auditing=False,
        gc_hijack_grace_period="30m",
        gc_failed_grace_period="5m",
        global_resource_check_timeout="30m",
        lidar_scanner_interval="15s",
        max_checks_per_second="30",
        enable_across_step=True,
        enable_p2p_volume_streaming=False,
        peer_address="{{ with node }}{{ .Node.Address }}{{ end }}",
        prometheus_bind_ip=IPv4Address("127.0.0.1"),
        prometheus_bind_port=CONCOURSE_PROMETHEUS_EXPORTER_DEFAULT_PORT,
        secret_cache_duration="1m",  # pragma: allowlist secret # noqa: S106
        secret_cache_enabled=True,  # pragma: allowlist secret
        tracing_otlp_address="localhost:4317",
        tracing_otlp_use_tls=False,
        vault_client_token="token-gets-overridden-by-vault-agent",  # noqa: S106
        vault_insecure_skip_verify=True,
        vault_path_prefix="/secret-concourse",
        vault_shared_path="shared",
        vault_url=f"http://localhost:{VAULT_HTTP_PORT}",
    ),
    CONCOURSE_WORKER_NODE_TYPE: partial(
        ConcourseWorkerConfig,
        additional_resource_types=["rclone", "s3-sync"],
        additional_resource_types_s3_location=(
            "ol-eng-artifacts.s3.amazonaws.com/bundled-concourse-resources"
        ),
        baggageclaim_bind_ip="0.0.0.0",  # noqa: S104
        baggageclaim_driver="overlay",
        bind_ip="0.0.0.0",  # noqa: S104
        connection_drain_timeout="5m",
        container_runtime="containerd",
        containerd_dns_server="8.8.8.8",
        containerd_max_containers=0,  # Don't set a limit on the number of containers
        containerd_network_pool="10.250.0.0/16",
        ephemeral=False,
    ),
}
concourse_config: ConcourseWebConfig | ConcourseWorkerConfig = concourse_config_map[
    node_type
]()
vault_template_map = {
    CONCOURSE_WEB_NODE_TYPE: [
        partial(
            VaultTemplate,
            contents=(
                '{{ with secret "secret-concourse/web" }}'
                "{{ printf .Data.data.tsa_private_key }}{{ end }}"
            ),
            destination=concourse_config.model_dump().get("tsa_host_key_path"),
        ),
        partial(
            VaultTemplate,
            contents=(
                '{{ with secret "secret-concourse/web" }}'
                "{{ printf .Data.data.session_signing_key }}{{ end }}"
            ),
            destination=concourse_config.model_dump().get("session_signing_key_path"),
        ),
        partial(
            VaultTemplate,
            contents=(
                '{{ with secret "secret-concourse/web" }}'
                "{{ printf .Data.data.worker_public_key }}{{ end }}"
            ),
            destination=concourse_config.model_dump().get("authorized_keys_file"),
        ),
    ],
    CONCOURSE_WORKER_NODE_TYPE: [
        partial(
            VaultTemplate,
            contents=(
                '{{ with secret "secret-concourse/worker" }}'
                "{{ printf .Data.data.worker_private_key }}{{ end }}"
            ),
            destination=concourse_config.model_dump().get("worker_private_key_path"),
        ),
        partial(
            VaultTemplate,
            contents=(
                '{{ with secret "secret-concourse/worker" }}'
                "{{ printf .Data.data.tsa_public_key }}{{ end }}"
            ),
            destination=concourse_config.model_dump().get("tsa_public_key_path"),
        ),
    ],
}

# Install Concourse
# Web nodes need awscli + jq for the stalled-worker reaper; workers need awscli
# for the spot/lifecycle drain helpers, plus qemu-user-static/binfmt-support so
# oci-build-task can cross-build linux/arm64 images via QEMU emulation.
install_baseline_packages(
    packages=["curl", "btrfs-progs"]
    + (
        ["awscli", "qemu-user-static", "binfmt-support"]
        if node_type == CONCOURSE_WORKER_NODE_TYPE
        else []
    )
    + (["awscli", "jq"] if node_type == CONCOURSE_WEB_NODE_TYPE else [])
)
concourse_install_changed = install_concourse(concourse_base_config)
concourse_config_changed = configure_concourse(concourse_config)

consul_configuration = {Path("00-default.json"): ConsulConfig()}

vector_config = VectorConfig()


if concourse_config._node_type == CONCOURSE_WEB_NODE_TYPE:  # noqa: SLF001
    # Setting this attribute after instantiating the object to bypass validation
    concourse_config.encryption_key = (
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

    # Install Traefik

    # Deploy main team OIDC config so CONCOURSE_MAIN_TEAM_CONFIG persists
    # across restarts
    files.directory(
        name="Create Concourse teams configuration directory",
        path="/etc/concourse/teams",
        user=concourse_config.user,
        present=True,
    )
    files.put(
        name="Upload main team OIDC configuration",
        src=str(FILES_DIRECTORY.joinpath("teams", "main.yaml")),
        dest="/etc/concourse/teams/main.yaml",
        user=concourse_config.user,
        mode="640",
    )

    traefik_config = TraefikConfig(
        version=VERSIONS["traefik"],
        static_configuration=traefik_static.TraefikStaticConfig.model_validate(
            yaml.safe_load(
                FILES_DIRECTORY.joinpath("traefik", "static_config.yaml").read_text()
            )
        ),
        file_configurations={
            Path(
                "concourse.yaml"
            ): traefik_file_provider.TraefikFileConfig.model_validate(
                yaml.safe_load(
                    FILES_DIRECTORY.joinpath(
                        "traefik", "dynamic_config.yaml"
                    ).read_text()
                )
            )
        },
    )
    install_traefik_binary(traefik_config)
    traefik_config_changed = configure_traefik(traefik_config)

    # Add vector configurations specific to concourse web nodes
    vector_config.configuration_templates[
        TEMPLATES_DIRECTORY.joinpath("vector", "concourse_logs.yaml")
    ] = {}
    vector_config.configuration_templates[
        TEMPLATES_DIRECTORY.joinpath("vector", "concourse_metrics.yaml")
    ] = {"concourse_prometheus_port": concourse_config.prometheus_bind_port}

    # I have no idea why but apparently we need to specify 'user' here as the
    # default from AlloyConfig isn't getting picked up
    alloy_config = AlloyConfig()
    # Install alloy
    install_and_configure_alloy(alloy_config)

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
                role=f"concourse-{concourse_config._node_type}"  # noqa: SLF001
            ),  # noqa: RUF100, SLF001
        ),
        sink=[VaultAutoAuthSink(type="file", config=[VaultAutoAuthFileSink()])],
    ),
    template=[partial_func() for partial_func in vault_template_map[node_type]]
    + [
        VaultTemplate(
            contents=get_template(
                io.StringIO(
                    "{% for env_var, env_value in concourse_env.items() -%}\n"
                    "{{ env_var }}={{ env_value }}\n{% endfor -%}"
                )
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

# Install vector
install_and_configure_vector(vector_config)


for product in hashicorp_products:
    configure_hashicorp_product(product)

# Upload worker scripts and service unit files before registering and starting
# concourse.service. The concourse.service unit carries Requires=/After= on
# concourse-worker-preflight.service for worker nodes; systemd will refuse to
# start concourse.service if that unit file does not exist at start time.
worker_services_changed = False
if node_type == CONCOURSE_WORKER_NODE_TYPE:
    worker_scripts = [
        ("concourse-worker-drain", "755"),
        ("concourse-worker-spot-watch", "755"),
        ("concourse-worker-lifecycle-hook", "755"),
        ("concourse-worker-preflight", "755"),
    ]
    for script_name, script_mode in worker_scripts:
        files.put(
            name=f"Upload {script_name} script",
            src=str(FILES_DIRECTORY.joinpath(script_name)),
            dest=f"/usr/local/bin/{script_name}",
            user="root",
            group="root",
            mode=script_mode,
        )
    # concourse-worker-preflight.service is uploaded here (so its unit file
    # is present and daemon-reload picks it up) but intentionally not
    # enabled/started below -- it's pulled in as a hard dependency of
    # concourse.service itself via Requires=/After= (see
    # concourse.service.j2), not run as an independent long-lived unit.
    worker_service_files = [
        "concourse-worker-spot-watch.service",
        "concourse-worker-lifecycle-hook.service",
        "concourse-worker-preflight.service",
    ]
    for service_file in worker_service_files:
        result = files.put(
            name=f"Upload {service_file} systemd unit",
            src=str(FILES_DIRECTORY.joinpath(service_file)),
            dest=f"/etc/systemd/system/{service_file}",
            user="root",
            group="root",
            mode="644",
        )
        worker_services_changed = worker_services_changed or result.changed

# Manage services
if host.get_fact(HasSystemd):
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
        traefik_service(traefik_config=traefik_config, do_reload=traefik_config_changed)

        # Install the stalled-worker reaper. It reconciles Concourse's worker
        # registry against EC2 and prunes "stalled" workers whose instance is
        # gone, recovering the cleanup behaviour of the `ephemeral` flag without
        # its cache-wiping hair-trigger. Runs on a 3-minute systemd timer.
        files.put(
            name="Upload concourse-worker-reaper script",
            src=str(FILES_DIRECTORY.joinpath("concourse-worker-reaper")),
            dest="/usr/local/bin/concourse-worker-reaper",
            user="root",
            group="root",
            mode="755",
        )
        reaper_unit_files = [
            "concourse-worker-reaper.service",
            "concourse-worker-reaper.timer",
        ]
        reaper_units_changed = False
        for unit_file in reaper_unit_files:
            result = files.put(
                name=f"Upload {unit_file} systemd unit",
                src=str(FILES_DIRECTORY.joinpath(unit_file)),
                dest=f"/etc/systemd/system/{unit_file}",
                user="root",
                group="root",
                mode="644",
            )
            reaper_units_changed = reaper_units_changed or result.changed
        # Enable the timer (not the service directly); the timer triggers the
        # oneshot service on its schedule.
        systemd.service(
            name="Enable and start concourse-worker-reaper timer",
            service="concourse-worker-reaper.timer",
            running=True,
            enabled=True,
            daemon_reload=reaper_units_changed,
        )
    else:
        watched_concourse_files.extend(
            [
                concourse_config.worker_private_key_path,
                concourse_config.tsa_public_key_path,
            ]
        )
        # Scripts and service unit files were already uploaded before
        # register_concourse_service (see block above HasSystemd), so only
        # the service enable/start calls remain here.
        for service_name in [
            "concourse-worker-spot-watch",
            "concourse-worker-lifecycle-hook",
        ]:
            systemd.service(
                name=f"Enable and start {service_name}",
                service=service_name,
                running=True,
                enabled=True,
                daemon_reload=worker_services_changed,
            )
    service_configuration_watches(
        service_name="concourse",
        watched_files=watched_concourse_files,
    )
