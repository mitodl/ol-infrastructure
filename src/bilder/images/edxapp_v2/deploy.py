import io
import json
import os
import tempfile
from pathlib import Path

from pyinfra import host
from pyinfra.operations import apt, files, server

from bilder.components.baseline.steps import service_configuration_watches
from bilder.components.hashicorp.consul.models import (
    Consul,
    ConsulAddresses,
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
)
from bilder.components.vector.models import VectorConfig
from bilder.components.vector.steps import install_and_configure_vector
from bilder.facts.has_systemd import HasSystemd
from bilder.images.edxapp_v2.lib import OPENEDX_RELEASE, WEB_NODE_TYPE, node_type
from bilder.lib.ami_helpers import build_tags_document
from bilder.lib.linux_helpers import DOCKER_COMPOSE_DIRECTORY
from bilder.lib.template_helpers import (
    CONSUL_TEMPLATE_DIRECTORY,
    place_consul_template_file,
)
from bridge.lib.magic_numbers import VAULT_HTTP_PORT
from bridge.lib.versions import (
    CONSUL_TEMPLATE_VERSION,
    CONSUL_VERSION,
    TUTOR_PERMISSIONS_VERSION,
    VAULT_VERSION,
)
from bridge.secrets.sops import set_env_secrets
from bridge.settings.openedx.accessors import fetch_application_version
from bridge.settings.openedx.types import OpenEdxApplication

VERSIONS = {
    "consul": os.environ.get("CONSUL_VERSION", CONSUL_VERSION),
    "consul-template": os.environ.get(
        "CONSUL_TEMPLATE_VERSION", CONSUL_TEMPLATE_VERSION
    ),
    "tutor_permissions": os.environ.get(
        "TUTOR_PERMISSIONS_VERSION", TUTOR_PERMISSIONS_VERSION
    ),
    "vault": os.environ.get("VAULT_VERSION", VAULT_VERSION),
}
TEMPLATES_DIRECTORY = Path(__file__).resolve().parent.joinpath("templates")
FILES_DIRECTORY = Path(__file__).resolve().parent.joinpath("files")

set_env_secrets(Path("consul/consul.env"))

vector_config = VectorConfig(is_docker=True, use_global_log_sink=True)
vector_config.configuration_templates[
    TEMPLATES_DIRECTORY.joinpath("vector", "edxapp_parsing.yaml.j2")
] = {}

# Setup some environment variables that will be pulled in by docker / docker-compose
EDX_INSTALLATION_NAME = os.environ.get("EDX_INSTALLATION", "mitxonline")
DOCKER_REPO_NAME = os.environ.get("DOCKER_REPO_NAME", "mitodl/edxapp")
DOCKER_IMAGE_DIGEST = os.environ.get("DOCKER_IMAGE_DIGEST")

production_staticfiles_archive_name = (
    f"staticfiles-production-{DOCKER_IMAGE_DIGEST}.tar.gz"
)
nonprod_staticfiles_archive_name = f"staticfiles-nonprod-{DOCKER_IMAGE_DIGEST}.tar.gz"

files.put(
    name=(
        "Setting the DOCKER_REPO_AND_DIGEST env var to"
        f" {DOCKER_REPO_NAME}@{DOCKER_IMAGE_DIGEST}"
    ),
    src=io.StringIO(f"{DOCKER_REPO_NAME}@{DOCKER_IMAGE_DIGEST}"),
    dest="/etc/default/docker_repo_and_digest",
)
files.put(
    name=(
        "Settings the TUTOR_PERMISSIONS_VERSION env var to"
        f" {VERSIONS['tutor_permissions']}"
    ),
    src=io.StringIO(VERSIONS["tutor_permissions"]),
    dest="/etc/default/tutor_permissions_tag",
)
files.put(
    name=f"Setting the COMPOSE_PROFILES variable to {node_type} for docker compose",
    src=io.StringIO(f"COMPOSE_PROFILES={node_type}"),
    dest="/etc/default/docker-compose",
)
files.line(
    name=f"Setting COMPOSE_PROFILES variable to {node_type} in the default profile",
    path="/etc/profile",
    line=f"\nexport COMPOSE_PROFILES={node_type}",
    present=True,
)

apt.packages(
    name="Remove unattended-upgrades to prevent race conditions during build",
    packages=["unattended-upgrades"],
    present=False,
)

# Preload docker image and staticfiles content. This will accelerate the first startup
server.shell(
    name=f"Preload {DOCKER_REPO_NAME}@{DOCKER_IMAGE_DIGEST}",
    commands=[f"/usr/bin/docker pull {DOCKER_REPO_NAME}@{DOCKER_IMAGE_DIGEST}"],
)
files.directory(
    name="Create production staticfiles directory",
    path="/opt/staticfiles-production",
    user="1000",
    group="1000",
    present=True,
)
files.directory(
    name="Create nonprod staticfiles directory",
    path="/opt/staticfiles-nonprod",
    user="1000",
    group="1000",
    present=True,
)
# Allow these steps to fail. This is expected while we transition to the Earthly builds
files.download(
    name=f"Download {production_staticfiles_archive_name}",
    src=f"https://ol-eng-artifacts.s3.amazonaws.com/edx-staticfiles/{EDX_INSTALLATION_NAME}/{OPENEDX_RELEASE}/{production_staticfiles_archive_name}",
    dest=f"/tmp/{production_staticfiles_archive_name}",  # noqa: S108
)
files.download(
    name=f"Download {nonprod_staticfiles_archive_name}",
    src=f"https://ol-eng-artifacts.s3.amazonaws.com/edx-staticfiles/{EDX_INSTALLATION_NAME}/{OPENEDX_RELEASE}/{nonprod_staticfiles_archive_name}",
    dest=f"/tmp/{nonprod_staticfiles_archive_name}",  # noqa: S108
)
server.shell(
    name=f"Extract {production_staticfiles_archive_name}",
    commands=[
        f"/usr/bin/tar -xf /tmp/{production_staticfiles_archive_name} "
        "--strip-components 2 -C /opt/staticfiles-production"
    ],
)
server.shell(
    name=f"Extract {nonprod_staticfiles_archive_name}",
    commands=[
        f"/usr/bin/tar -xf /tmp/{nonprod_staticfiles_archive_name} "
        "--strip-components 2 -C /opt/staticfiles-nonprod"
    ],
)


# Create skeleton directory structures for docker-compose
shared_data_directory = Path("/opt/data")
lms_data_directory = Path("/opt/data/lms")
cms_data_directory = Path("/opt/data/cms")
media_directory = shared_data_directory.joinpath("media")
lms_tracking_logs_directory = lms_data_directory.joinpath("logs")
cms_tracking_logs_directory = cms_data_directory.joinpath("logs")
settings_directory = DOCKER_COMPOSE_DIRECTORY.joinpath("settings")
tls_directory = DOCKER_COMPOSE_DIRECTORY.joinpath("tls")
ssh_directory = DOCKER_COMPOSE_DIRECTORY.joinpath("ssh")

files.directory(
    name="Create docker compose directory",
    path=str(DOCKER_COMPOSE_DIRECTORY),
    user="root",
    group="root",
    present=True,
)

files.directory(
    name="Create LMS data directory",
    path=lms_data_directory,
    user="1000",
    group="1000",
    present=True,
)
files.directory(
    name="Create CMS data directory",
    path=cms_data_directory,
    user="1000",
    group="1000",
    present=True,
)
files.directory(
    name="Create media directory",
    path=media_directory,
    user="1000",
    group="1000",
    present=True,
)
files.directory(
    name="Create settings directory",
    path=settings_directory,
    user="root",
    group="root",
    present=True,
)
files.directory(
    name="Create TLS directory",
    path=tls_directory,
    user="root",
    group="root",
    present=True,
)
files.directory(
    name="Create SSH directory",
    path=ssh_directory,
    user="1000",
    group="1000",
    mode="0700",
    present=True,
)
files.directory(
    name="Create LMS tracking logs directory",
    path=lms_tracking_logs_directory,
    user="1000",
    group="1000",
    present=True,
)
files.directory(
    name="Create CMS tracking logs directory",
    path=cms_tracking_logs_directory,
    user="1000",
    group="1000",
    present=True,
)

watched_files: list[Path] = []
consul_templates: list[ConsulTemplateTemplate] = []

# Firstly place down normal files requiring no special templating activities
untemplated_files = {
    "Caddyfile": settings_directory,
    "uwsgi.ini": settings_directory,
}

for ut_filename, dest_dir in untemplated_files.items():
    files.put(
        name=f"Place {ut_filename} file",
        src=str(FILES_DIRECTORY.joinpath(ut_filename)),
        dest=str(dest_dir.joinpath(ut_filename)),
        mode="0664",
    )
    watched_files.append(dest_dir.joinpath(ut_filename))

## Place down not-special consul-template files
## Assume a .tmpl file extension that will be retained
consul_templated_files = {
    "docker-compose.yaml": (DOCKER_COMPOSE_DIRECTORY, "0660"),
    ".env": (DOCKER_COMPOSE_DIRECTORY, "0660"),
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

# Initalize the default consul configuration
consul_configuration = {
    Path("00-default.json"): ConsulConfig(
        addresses=ConsulAddresses(dns="127.0.0.1", http="127.0.0.1"),
        advertise_addr="{{ GetPrivateIP }}",
        services=[],
    )
}

# Add an inline consul-template for rendering the waffle_flags.yaml file
consul_templates.append(
    ConsulTemplateTemplate(
        contents='{{ key "edxapp/waffle_flags.yaml" }}',
        destination=settings_directory.joinpath("waffle_flags.yaml"),
    )
)

# Create a few in-line consul templates for the wildcard certificate + key
# but only do this on webnodes to limit distribution of secrets
if node_type == WEB_NODE_TYPE:
    tls_certificate_file = tls_directory.joinpath("certificate")
    consul_templates.append(
        ConsulTemplateTemplate(
            contents=(
                '{{ with secret "secret-'
                + EDX_INSTALLATION_NAME
                + "/"
                + EDX_INSTALLATION_NAME
                + '-wildcard-certificate" }}{{ printf .Data.cert }}{{ end }}'
            ),
            destination=tls_certificate_file,
        ),
    )
    tls_key_file = tls_directory.joinpath("key")
    consul_templates.append(
        ConsulTemplateTemplate(
            contents=(
                '{{ with secret "secret-'
                + EDX_INSTALLATION_NAME
                + "/"
                + EDX_INSTALLATION_NAME
                + '-wildcard-certificate" }}{{ printf .Data.key }}{{ end }}'
            ),
            destination=tls_key_file,
        ),
    )

    watched_files.extend([tls_certificate_file, tls_key_file])

    # Additionally, on web nodes create the consul service for edxapp
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
        ],
    )
    vector_config.configuration_templates[
        TEMPLATES_DIRECTORY.joinpath("vector", "edxapp_tracking_logs.yaml.j2")
    ] = {}

# Setup the ssh key for git-auto-export-plugin
ssh_key_file = ssh_directory.joinpath("id_rsa")
consul_templates.append(
    ConsulTemplateTemplate(
        contents=(
            '{{with secret "secret-operations/global/github-enterprise-ssh" }}'
            "{{ printf .Data.private_key }}{{ end }}"
        ),
        group="1000",
        user="1000",
        perms="0600",
        destination=ssh_key_file,
    )
)
watched_files.append(ssh_key_file)

# Setup the lms.env.yml and cms.env.yml files for edxapp
EDX_TEMPLATES_DIRECTORY = TEMPLATES_DIRECTORY.joinpath("edxapp", EDX_INSTALLATION_NAME)

common_config = EDX_TEMPLATES_DIRECTORY.joinpath("common_values.yml.tmpl")

lms_config = EDX_TEMPLATES_DIRECTORY.joinpath("lms_only.yml.tmpl")
lms_config_consul_template_file = Path(CONSUL_TEMPLATE_DIRECTORY).joinpath(
    "lms.env.yml.tmpl"
)
lms_config_file = settings_directory.joinpath("lms.env.yml")
with tempfile.NamedTemporaryFile("wt", delete=False) as lms_template:
    lms_template.write(common_config.read_text())
    lms_template.write(lms_config.read_text())
    files.put(
        name="Upload lms.env.yml consul template",
        src=lms_template.name,
        dest=str(lms_config_consul_template_file),
        user="consul-template",
        group="consul-template",
        create_remote_dir=True,
    )
consul_templates.append(
    ConsulTemplateTemplate(
        source=lms_config_consul_template_file,
        destination=lms_config_file,
        perms="0664",
    )
)
watched_files.append(lms_config_file)

cms_config = EDX_TEMPLATES_DIRECTORY.joinpath("cms_only.yml.tmpl")
cms_config_consul_template_file = Path(CONSUL_TEMPLATE_DIRECTORY).joinpath(
    "cms.env.yml.tmpl"
)
cms_config_file = settings_directory.joinpath("cms.env.yml")
with tempfile.NamedTemporaryFile("wt", delete=False) as cms_template:
    cms_template.write(common_config.read_text())
    cms_template.write(cms_config.read_text())
    files.put(
        name="Upload cms.env.yml consul template",
        src=cms_template.name,
        dest=str(cms_config_consul_template_file),
        user="consul-template",
        group="consul-template",
        create_remote_dir=True,
    )
consul_templates.append(
    ConsulTemplateTemplate(
        source=cms_config_consul_template_file,
        destination=cms_config_file,
        perms="0664",
    )
)
watched_files.append(cms_config_file)

## Install and Configure Consul and Vault
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
            mount_path=f"auth/aws-{EDX_INSTALLATION_NAME}",
            config=VaultAutoAuthAWS(role=f"edxapp-{node_type}"),
        ),
        sink=[VaultAutoAuthSink(type="file", config=[VaultAutoAuthFileSink()])],
    ),
    template=None,  # Thou shalt not use vault-template
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
# The docker-baseline ami is missing consul_template by default
install_hashicorp_products([consul_template])

hashicorp_products = [vault, consul, consul_template]
for product in hashicorp_products:
    configure_hashicorp_product(product)

consul_template_permissions(consul_template.configuration)

## Install and configure vector
install_and_configure_vector(vector_config)

# Place the tags document
edx_platform = fetch_application_version(
    OPENEDX_RELEASE, EDX_INSTALLATION_NAME, OpenEdxApplication.edxapp
)
theme = fetch_application_version(
    OPENEDX_RELEASE, EDX_INSTALLATION_NAME, OpenEdxApplication.theme
)
edx_platform_sha = os.environ.get("EDXAPP_COMMIT_SHA", edx_platform.release_branch)
theme_sha = os.environ.get("EDX_THEME_COMMIT_SHA", theme.release_branch)

tags_json = json.dumps(
    build_tags_document(
        source_tags={
            "consul_version": VERSIONS["consul"],
            "consul_template_version": VERSIONS["consul-template"],
            "vault_version": VERSIONS["vault"],
            "docker_repo": DOCKER_REPO_NAME,
            "docker_digest": DOCKER_IMAGE_DIGEST,
            "edxapp_repo": edx_platform.git_origin,
            "edxapp_branch": edx_platform.release_branch,
            "edxapp_sha": edx_platform_sha,
            "theme_repo": theme.git_origin,
            "theme_branch": theme.release_branch,
            "theme_sha": theme_sha,
        }
    )
)
files.put(
    name="Place the tags document at /etc/ami_tags.json",
    src=io.StringIO(tags_json),
    dest="/etc/ami_tags.json",
    mode="0644",
    user="root",
)

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
