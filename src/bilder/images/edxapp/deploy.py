import os
import tempfile
from pathlib import Path

from pyinfra import host
from pyinfra.operations import apt, files, git, pip, server

from bilder.components.baseline.steps import service_configuration_watches
from bilder.components.hashicorp.consul.models import (
    Consul,
    ConsulConfig,
    ConsulService,
    ConsulServiceTCPCheck,
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
from bilder.images.edxapp.lib import WEB_NODE_TYPE, node_type
from bilder.images.edxapp.plugins.git_export_import import git_auto_export
from bridge.lib.magic_numbers import VAULT_HTTP_PORT
from bridge.lib.versions import CONSUL_TEMPLATE_VERSION, CONSUL_VERSION, VAULT_VERSION
from bridge.secrets.sops import set_env_secrets

VERSIONS = {
    "consul": CONSUL_VERSION,
    "vault": VAULT_VERSION,
    "consul-template": CONSUL_TEMPLATE_VERSION,
}
TEMPLATES_DIRECTORY = Path(__file__).resolve().parent.joinpath("templates")
EDX_INSTALLATION_NAME = os.environ.get("EDX_INSTALLATION", "mitxonline")
EDX_USER = "edxapp"
set_env_secrets(Path("consul/consul.env"))

apt.packages(
    name="Remove unattended-upgrades to prevent race conditions during build",
    packages=["unattended-upgrades"],
    present=False,
)

###########
# edX App #
###########
# Check out desired repository and branch for edx-platform. This lets us manage our
# custom code without having to bake it into the base image.
git_remote = host.data.edx_platform_repository[EDX_INSTALLATION_NAME]["origin"]
git_branch = host.data.edx_platform_repository[EDX_INSTALLATION_NAME]["branch"]
edx_platform_path = "/edx/app/edxapp/edx-platform/"
server.shell(
    name="Fix git",
    commands=["git config --global --add safe.directory /edx/app/edxapp/edx-platform"],
    _chdir=edx_platform_path,
)

server.shell(
    name="Ensure the edx-platform git origin is configured",
    commands=[f"git remote add custom {git_remote}", "git fetch --all --prune --tags"],
    _chdir=edx_platform_path,
)
git.repo(
    name="Check out the desired branch",
    src=git_remote,
    dest=edx_platform_path,
    branch=git_branch,
    pull=False,
    user=EDX_USER,
    group=EDX_USER,
)

git_auto_export()
# Install additional Python dependencies for use with edxapp
pip.packages(
    name="Install additional edX dependencies",
    packages=host.data.edx_plugins_added[EDX_INSTALLATION_NAME],
    present=True,
    virtualenv="/edx/app/edxapp/venvs/edxapp/",
    _sudo_user=EDX_USER,
)

pip.packages(
    name="Uninstall undesired edX dependencies",
    packages=host.data.edx_plugins_removed.get(EDX_INSTALLATION_NAME, []),
    present=False,
    virtualenv="/edx/app/edxapp/venvs/edxapp/",
    _sudo_user=EDX_USER,
)

files.directory(
    name="Create edX log directory and set permissions",
    path=str(Path("/var/log/edxapp/")),
    present=True,
    mode="0775",
    user="www-data",
    group=EDX_USER,
    recursive=True,
)

# Ugly, hopefully short-term, fix for getting the beat scheduler working again in ODL edx deployments.
files.line(
    name="Fix beat_scheduler.sh to invoke RedBeat rather than the broken single-beat definition.",
    path=str(Path("/edx/app/edxapp/beat_scheduler.sh")),
    line="^exec.*$",
    replace="exec /edx/app/edxapp/venvs/edxapp/bin/celery beat $@",
    backup=True,
)

apt.packages(
    name="Install ACL package for more granular file permissions",
    packages=["acl"],
    present=True,
)
log_file = Path("/var/log/edxapp/app.log")
server.shell(
    name="Allow edxapp and www-data users to write to the log file",
    commands=[
        f"setfacl -R -d -m u:edxapp:rwx {log_file.parent}",
        f"setfacl -R -d -m u:www-data:rwx {log_file.parent}",
    ],
)
vector_config = VectorConfig()
vector_config.configuration_templates[
    TEMPLATES_DIRECTORY.joinpath("vector", "edxapp_logs.yaml.j2")
] = {"edx_installation": EDX_INSTALLATION_NAME}
vector_config.configuration_templates[
    TEMPLATES_DIRECTORY.joinpath("vector", "edxapp_metrics.yaml.j2")
] = {"edx_installation": EDX_INSTALLATION_NAME}
consul_configuration = {Path("00-default.json"): ConsulConfig()}

# Manage Vault templates
vault_templates = [
    VaultTemplate(
        contents=(
            '{{with secret "secret-operations/global/github-enterprise-ssh" }}'
            "{{ printf .Data.private_key }}{{ end }}"
        ),
        destination=Path("/var/www/.ssh/id_rsa"),
    )
]

# Set up Consul templates
lms_config_path = Path("/edx/etc/lms.yml")
studio_config_path = Path("/edx/etc/studio.yml")
forum_config_path = Path("/edx/app/forum/forum_env")
lms_intermediate_template = Path("/etc/consul-template/templates/edxapp-lms.tmpl")
studio_intermediate_template = Path("/etc/consul-template/templates/edxapp-studio.tmpl")
forum_intermediate_template = Path("/etc/consul-template/templates/edx-forum.tmpl")
consul_templates = [
    ConsulTemplateTemplate(
        source=studio_intermediate_template,
        destination=studio_config_path,
    ),
    ConsulTemplateTemplate(
        source=lms_intermediate_template, destination=lms_config_path
    ),
]
if node_type == WEB_NODE_TYPE:
    files.put(
        name="Set up Nginx status endpoint for metrics collection",
        src=str(Path(__file__).resolve().parent.joinpath("files", "nginx_status.conf")),
        dest=str(Path("/etc/nginx/sites-enabled/status_monitor")),
        user="www-data",
        group="www-data",
    )
    vector_config.configuration_templates[
        TEMPLATES_DIRECTORY.joinpath("vector", "edxapp_tracking_logs.yaml.j2")
    ] = {}
    vault_templates.extend(
        [
            VaultTemplate(
                contents=(
                    '{{ with secret "secret-'
                    + EDX_INSTALLATION_NAME
                    + "/"
                    + EDX_INSTALLATION_NAME
                    + '-wildcard-certificate" }}'
                    "{{ printf .Data.cert_chain }}{{ end }}"
                ),
                destination=Path("/etc/ssl/certs/edxapp.cert"),
            ),
            VaultTemplate(
                contents=(
                    '{{ with secret "secret-'
                    + EDX_INSTALLATION_NAME
                    + "/"
                    + EDX_INSTALLATION_NAME
                    + '-wildcard-certificate" }}'
                    "{{ printf .Data.key }}{{ end }}"
                ),
                destination=Path("/etc/ssl/private/edxapp.key"),
            ),
        ]
    )
    consul_templates.extend(
        [
            ConsulTemplateTemplate(
                source=forum_intermediate_template, destination=forum_config_path
            ),
        ]
    )
    consul_configuration[Path("01-edxapp.json")] = ConsulConfig(
        services=[
            ConsulService(
                name="edxapp",
                port=8000,
                tags=["lms"],
                check=ConsulServiceTCPCheck(
                    name="edxapp-lms",
                    tcp="localhost:8000",
                    interval="10s",
                ),
            ),
            ConsulService(
                name="forum",
                port=4567,
                check=ConsulServiceTCPCheck(
                    name="edxapp-forum",
                    tcp="localhost:4567",
                    interval="10s",
                ),
            ),
        ]
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
            mount_path=f"auth/aws-{EDX_INSTALLATION_NAME}",
            config=VaultAutoAuthAWS(role=f"edxapp-{node_type}"),
        ),
        sink=[VaultAutoAuthSink(type="file", config=[VaultAutoAuthFileSink()])],
    ),
    template=vault_templates,
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
hashicorp_products = [vault, consul, consul_template]
install_hashicorp_products(hashicorp_products)
# Install Vector log agent
install_vector(vector_config)

# Upload templates for consul-template agent
EDX_TEMPLATES_DIRECTORY = TEMPLATES_DIRECTORY.joinpath("edxapp", EDX_INSTALLATION_NAME)
common_config = EDX_TEMPLATES_DIRECTORY.joinpath("common_values.yml.tmpl")
studio_config = EDX_TEMPLATES_DIRECTORY.joinpath("studio_only.yml.tmpl")
lms_config = EDX_TEMPLATES_DIRECTORY.joinpath("lms_only.yml.tmpl")
forum_config = EDX_TEMPLATES_DIRECTORY.joinpath("forum.env")

with tempfile.NamedTemporaryFile("wt", delete=False) as studio_template:
    studio_template.write(common_config.read_text())
    studio_template.write(studio_config.read_text())
    files.put(
        name="Upload studio.yml template for Vault agent",
        src=studio_template.name,
        dest=str(studio_intermediate_template),
        user=consul_template.name,
        group=consul_template.name,
        create_remote_dir=True,
    )
with tempfile.NamedTemporaryFile("wt", delete=False) as lms_template:
    lms_template.write(common_config.read_text())
    lms_template.write(lms_config.read_text())
    files.put(
        name="Upload lms.yml template for consul-template agent",
        src=lms_template.name,
        dest=str(lms_intermediate_template),
        user=consul_template.name,
        group=consul_template.name,
        create_remote_dir=True,
    )
with tempfile.NamedTemporaryFile("wt", delete=False) as forum_template:
    forum_template.write(forum_config.read_text())
    files.put(
        name="Upload forum_env template for consul-template agent",
        src=forum_template.name,
        dest=str(forum_intermediate_template),
        user=consul_template.name,
        group=consul_template.name,
        create_remote_dir=True,
    )

# Manage services
if host.get_fact(HasSystemd):
    supervisor_command = "signal HUP" if node_type == WEB_NODE_TYPE else "restart"
    register_services(hashicorp_products, start_services_immediately=False)
    proxy_consul_dns()
    vector_service(vector_config)
    service_configuration_watches(
        service_name="nginx",
        watched_files=[Path("/etc/ssl/certs/edxapp.pem")],
        start_now=False,
    )
    service_configuration_watches(
        service_name="edxapp-lms",
        watched_files=[lms_config_path],
        start_now=False,
        onchange_command=(
            # Let edxapp read the rendered config file
            f"/bin/bash -c 'chown edxapp:www-data {lms_config_path} && "
            # Ensure that Vault can update the file when credentials refresh
            f"setfacl -m u:consul-template:rwx {lms_config_path} && "
            f"setfacl -m u:edxapp:rwx {lms_config_path} && "
            # Restart the edxapp process to reload the configuration file
            f"/edx/bin/supervisorctl {supervisor_command} "
            f"{'lms' if node_type == WEB_NODE_TYPE else 'all'}'"
        ),
    )
    service_configuration_watches(
        service_name="edxapp-cms",
        watched_files=[studio_config_path],
        start_now=False,
        onchange_command=(
            # Let edxapp read the rendered config file
            f"/bin/bash -c 'chown edxapp:www-data {studio_config_path} && "
            # Ensure that Vault can update the file when credentials refresh
            f"setfacl -m u:consul-template:rwx {studio_config_path} && "
            f"setfacl -m u:edxapp:rwx {studio_config_path} && "
            # Restart the edxapp process to reload the configuration file
            f"/edx/bin/supervisorctl {supervisor_command} "
            f"{'cms' if node_type == WEB_NODE_TYPE else 'all'}'"
        ),
    )
    service_configuration_watches(
        service_name="edxapp-forum",
        watched_files=[forum_config_path],
        start_now=False,
        onchange_command=(
            # Let forum read the rendered config file
            f"/bin/bash -c 'chown forum:www-data {forum_config_path} && "
            # Ensure that consul-template can update the file when credentials refresh
            f"setfacl -m u:consul-template:rwx {forum_config_path} && "
            f"setfacl -m u:forum:rwx {forum_config_path} && "
            # Restart the forum process to reload the configuration file
            "/edx/bin/supervisorctl restart forum'"
        ),
    )

if "mitodl" in git_remote and node_type == WEB_NODE_TYPE:
    # Recompile static assets to ensure that any JS tweaks are rendered at runtime.
    server.shell(
        name="Compile static assets for Canvas integration",
        commands=["/edx/bin/edxapp-update-assets"],
    )

if node_type == WEB_NODE_TYPE and EDX_INSTALLATION_NAME in {"mitx", "mitx-staging"}:
    server.shell(
        name="Allow xqueue user to always read config file",
        commands=[
            "setfacl -R -d -m u:xqueue:r /edx/etc/",
        ],
    )
    xqueue_config_path = Path("/edx/etc/xqueue.yml")
    xqueue_template_path = Path("/etc/consul-template/templates/xqueue.tmpl")
    consul.configuration[Path("02-xqueue.json")] = ConsulConfig(
        services=[
            ConsulService(
                name="xqueue",
                port=18040,
                check=ConsulServiceTCPCheck(
                    name="edxapp-xqueue",
                    tcp="localhost:8040",
                    interval="10s",
                ),
            ),
        ]
    )
    xqueue_config = EDX_TEMPLATES_DIRECTORY.joinpath("xqueue.yml.tmpl")
    with tempfile.NamedTemporaryFile("wt", delete=False) as xqueue_template:
        xqueue_template.write(xqueue_config.read_text())
        files.put(
            name="Upload xqueue config template for consul-template agent",
            src=xqueue_template.name,
            dest=str(xqueue_template_path),
            user=consul_template.name,
            group=consul_template.name,
            create_remote_dir=True,
        )
    consul_template.configuration[Path("02-xqueue.json")] = ConsulTemplateConfig(
        template=[
            ConsulTemplateTemplate(
                source=xqueue_template_path, destination=xqueue_config_path
            ),
        ]
    )
    service_configuration_watches(
        service_name="edxapp-xqueue",
        watched_files=[xqueue_config_path],
        start_now=False,
        onchange_command=(
            # Let forum read the rendered config file
            f"/bin/bash -c 'chown xqueue:www-data {xqueue_config_path} && "
            # Ensure that consul-template can update the file when credentials refresh
            f"setfacl -m u:consul-template:rwx {xqueue_config_path} && "
            f"setfacl -m u:xqueue:rwx {xqueue_config_path} && "
            # Restart the xqueue process to reload the configuration file
            "/edx/bin/supervisorctl restart xqueue && "
            "/edx/bin/supervisorctl restart xqueue_consumer'"
        ),
    )

configure_vector(vector_config)
for product in hashicorp_products:
    configure_hashicorp_product(product)
vault_template_permissions(vault_config)
consul_template_permissions(consul_template.configuration)
