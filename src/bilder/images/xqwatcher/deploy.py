# ruff: noqa: E501

import os
from pathlib import Path

from pyinfra import host
from pyinfra.operations import apt, files, git, server

from bilder.components.baseline.steps import (
    install_baseline_packages,
    service_configuration_watches,
)
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
    configure_hashicorp_products,
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
from bilder.components.vector.steps import (
    install_and_configure_vector,
    vector_service,
)
from bilder.facts.has_systemd import HasSystemd
from bridge.lib.magic_numbers import VAULT_HTTP_PORT
from bridge.lib.versions import CONSUL_TEMPLATE_VERSION, CONSUL_VERSION, VAULT_VERSION
from bridge.secrets.sops import set_env_secrets

DEPLOYMENT = os.environ["DEPLOYMENT"]
if DEPLOYMENT not in ["mitxonline", "mitx", "mitx-staging", "xpro"]:
    msg = "DEPLOYMENT should be set to one of these values: 'mitxonline', 'mitx', 'mitx-staging', 'xpro'"
    raise ValueError(msg)

VERSIONS = {
    "consul": os.environ.get("CONSUL_VERSION", CONSUL_VERSION),
    "consul-template": os.environ.get(
        "CONSUL_TEMPLATE_VERSION", CONSUL_TEMPLATE_VERSION
    ),
    "vault": os.environ.get("VAULT_VERSION", VAULT_VERSION),
}
set_env_secrets(Path("consul/consul.env"))

FILES_DIRECTORY = Path(__file__).resolve().parent.joinpath("files")
TEMPLATES_DIRECTORY = Path(__file__).resolve().parent.joinpath("templates")

install_baseline_packages(
    packages=[
        "build-essential",
        "cron",
        "curl",
        "gfortran",
        "git",
        "jq",
        "libatlas-base-dev",
        "libopenblas64-0",
        "liblapack-dev",
        "libmariadb-dev",
        "libopenblas-dev",
        "libssl-dev",
        "logrotate",
        "pkg-config",
        "python3",
        "python3-dev",
        "python3-pip",
        "python3-virtualenv",
    ],
    upgrade_system=True,
)

server.shell(
    name="Disable git safe directory checking on immutable machines",
    commands=["git config --system safe.directory *"],
)

apt.packages(
    name="Remove unattended-upgrades to prevent race conditions during build",
    packages=["unattended-upgrades"],
    present=False,
)

APP_ARMOR_DIR = Path("/etc/apparmor.d")

XQWATCHER_HOME = Path("/home/xqwatcher")
XQWATCHER_SSH_DIR = XQWATCHER_HOME.joinpath(".ssh")
XQWATCHER_INSTALL_DIR = XQWATCHER_HOME.joinpath("xqwatcher")

XQWATCHER_VENV_DIR = XQWATCHER_INSTALL_DIR.joinpath(".venv")
XQWATCHER_CONF_DIR = XQWATCHER_INSTALL_DIR.joinpath("conf.d")
XQWATCHER_LOG_DIR = XQWATCHER_INSTALL_DIR.joinpath("log")
XQWATCHER_GRADERS_DIR = XQWATCHER_INSTALL_DIR.joinpath("graders")
XQWATCHER_GRADERS_VENVS_DIR = XQWATCHER_INSTALL_DIR.joinpath("grader_venvs")
XQWATCHER_GRADERS_CONFIG_FILE = XQWATCHER_CONF_DIR.joinpath("grader_config.json")
XQWATCHER_FETCH_GRADERS_CONFIG_FILE = XQWATCHER_INSTALL_DIR.joinpath(
    "fetch_graders.yaml"
)
XQWATCHER_FETCH_GRADERS_SCRIPT_FILE = XQWATCHER_INSTALL_DIR.joinpath("fetch_graders.py")
XQWATCHER_LOGGING_CONFIG_FILE = XQWATCHER_INSTALL_DIR.joinpath("logging.json")

XQWATCHER_SERVICE_FILE = Path("/usr/lib/systemd/system/xqwatcher.service")
XQWATCHER_BRANCH = "master"
XQWATCHER_GIT_REPO = "https://github.com/mitodl/xqueue-watcher.git"
XQWATCHER_USER = "xqwatcher"

shared_template_context = {
    "XQWATCHER_FETCH_GRADERS_CONFIG_FILE": str(XQWATCHER_FETCH_GRADERS_CONFIG_FILE),
    "XQWATCHER_GRADERS_DIR": str(XQWATCHER_GRADERS_DIR),
    "XQWATCHER_GRADERS_VENVS_DIR": str(XQWATCHER_GRADERS_VENVS_DIR),
    "XQWATCHER_INSTALL_DIR": str(XQWATCHER_INSTALL_DIR),
    "XQWATCHER_LOG_DIR": str(XQWATCHER_LOG_DIR),
    "XQWATCHER_USER": str(XQWATCHER_USER),
    "XQWATCHER_VENV_DIR": str(XQWATCHER_VENV_DIR),
}

server.user(  # noqa: S604
    name="Create xqwatcher user and home directory",
    create_home=True,
    ensure_home=True,
    home=str(XQWATCHER_HOME),
    present=True,
    shell="/bin/false",
    user=XQWATCHER_USER,
)

git.repo(
    name="Clone xqwatcher repository from github",
    branch=XQWATCHER_BRANCH,
    dest=str(XQWATCHER_INSTALL_DIR),
    group=XQWATCHER_USER,
    pull=True,
    src=XQWATCHER_GIT_REPO,
    user=XQWATCHER_USER,
)

files.directory(
    name="Remove the existing conf.d directory for xqwatcher configurations",
    path=str(XQWATCHER_CONF_DIR),
    force=True,
    present=False,
)

server.shell(
    name="Create virtual environment for xqwatcher",
    commands=[f"/usr/bin/virtualenv {XQWATCHER_VENV_DIR} --always-copy"],
)

files.put(
    name="Place custom xqwatcher requirements file.",
    src=str(FILES_DIRECTORY.joinpath("requirements.txt")),
    dest=str(XQWATCHER_INSTALL_DIR.joinpath("requirements.txt")),
    mode="0664",
    user=XQWATCHER_USER,
    group=XQWATCHER_USER,
)

server.shell(
    name="Install xqwatcher requirements into the virtual environment",
    commands=[
        f"{XQWATCHER_VENV_DIR.joinpath('bin/pip3')!s} install -r {XQWATCHER_INSTALL_DIR.joinpath('requirements.txt')!s} --no-cache-dir --exists-action w"
    ],
)

files.directory(
    name="Create a new conf.d directory for xqwatcher configurations",
    path=str(XQWATCHER_CONF_DIR),
    user=XQWATCHER_USER,
    group=XQWATCHER_USER,
    present=True,
    mode="0750",
)

files.directory(
    name="Create a directory for logs",
    path=str(XQWATCHER_LOG_DIR),
    user=XQWATCHER_USER,
    group=XQWATCHER_USER,
    mode="0750",
)

files.template(
    name="Create logging configuration file",
    src=TEMPLATES_DIRECTORY.joinpath("logging.json.j2"),
    dest=str(XQWATCHER_LOGGING_CONFIG_FILE),
    user=XQWATCHER_USER,
    group=XQWATCHER_USER,
    mode="0664",
    shared_context=shared_template_context,
)

files.template(
    name="Create logrotate.d configuration",
    src=str(TEMPLATES_DIRECTORY.joinpath("logrotate.xqwatcher.j2")),
    dest="/etc/logrotate.d/xqwatcher",
    user="root",
    group="root",
    mode="0644",
    shared_context=shared_template_context,
)

files.template(
    name="Create grader fetch script",
    src=TEMPLATES_DIRECTORY.joinpath("fetch_graders.py.j2"),
    dest=str(XQWATCHER_FETCH_GRADERS_SCRIPT_FILE),
    user=XQWATCHER_USER,
    group=XQWATCHER_USER,
    mode="0754",
    shared_context=shared_template_context,
)

server.crontab(
    name="Schedule fetch_graders.py to run every hour.",
    command=f"{XQWATCHER_VENV_DIR}/bin/python3 {XQWATCHER_FETCH_GRADERS_SCRIPT_FILE}",
    user=XQWATCHER_USER,
    cron_name="fetch_graders",
    minute="10",
)

files.directory(
    name="Create grader directory",
    path=str(XQWATCHER_GRADERS_DIR),
    user=XQWATCHER_USER,
    group=XQWATCHER_USER,
    mode="0750",
)

files.directory(
    name="Create grader venvs directory",
    path=str(XQWATCHER_GRADERS_VENVS_DIR),
    user=XQWATCHER_USER,
    group=XQWATCHER_USER,
    mode="0750",
)

files.directory(
    name="Create ~xqwatcher/.ssh directory",
    path=str(XQWATCHER_SSH_DIR),
    user=XQWATCHER_USER,
    group=XQWATCHER_USER,
    mode="0700",
)

files.template(
    name="Create systemd service definition for xqwatcher.",
    src=TEMPLATES_DIRECTORY.joinpath("xqwatcher.service.j2"),
    dest=str(XQWATCHER_SERVICE_FILE),
    user="root",
    group="root",
    mode="0644",
    shared_context=shared_template_context,
)

files.put(
    name="Create xqwatcher.json",
    src=str(FILES_DIRECTORY.joinpath("xqwatcher.json")),
    dest=str(XQWATCHER_INSTALL_DIR.joinpath("xqwatcher.json")),
    user=XQWATCHER_USER,
    group=XQWATCHER_USER,
    mode="0644",
)

files.template(
    name="Install xqwatcher-pkill sudoer entry",
    src=str(TEMPLATES_DIRECTORY.joinpath("98-xqwatcher-pkill.j2")),
    dest="/etc/sudoers.d/98-xqwatcher-pkill",
    user="root",
    group="root",
    mode="0600",
    shared_context=shared_template_context,
)

grader_venvs = ["mit-600x", "mit-686x-mooc", "mit-686x"]
for grader_venv in grader_venvs:
    GRADER_VENV_DIR = XQWATCHER_GRADERS_VENVS_DIR.joinpath(grader_venv)
    GRADER_REQS_FILE = GRADER_VENV_DIR.joinpath("requirements.txt")
    server.user(  # noqa: S604
        name=f"Install grader {grader_venv} : Create user",
        home="/dev/null",
        ensure_home=False,
        create_home=False,
        present=True,
        shell="/bin/false",
        user=grader_venv,
        groups=[XQWATCHER_USER],
    )
    server.shell(
        name=f"Install grader {grader_venv} : Create virtual environment",
        commands=[f"/usr/bin/virtualenv {GRADER_VENV_DIR} --always-copy"],
    )
    files.put(
        name=f"Install grader {grader_venv} : create requirements file",
        src=str(FILES_DIRECTORY.joinpath(f"grader_reqs/{grader_venv}.txt")),
        dest=str(GRADER_REQS_FILE),
        mode="0664",
        user=XQWATCHER_USER,
        group=XQWATCHER_USER,
    )
    files.template(
        name=f"Install grader {grader_venv} : create app-armor profile",
        src=str(TEMPLATES_DIRECTORY.joinpath("app_armor_profile.j2")),
        dest=str(APP_ARMOR_DIR.joinpath(f"xqwatcher.{grader_venv}")),
        user="root",
        group="root",
        mode="0644",
        grader_context={"GRADER_VENV_DIR": str(GRADER_VENV_DIR)},
    )
    files.template(
        name=f"Install grader {grader_venv} : Create sudoer entry",
        src=str(TEMPLATES_DIRECTORY.joinpath("99-xqwatcher-grader.j2")),
        dest=f"/etc/sudoers.d/99-xqwatcher-{grader_venv}",
        user="root",
        group="root",
        mode="0600",
        shared_context=shared_template_context,
        grader_context={
            "GRADER_VENV_DIR": str(GRADER_VENV_DIR),
            "GRADER_USER": grader_venv,
        },
    )
    server.shell(
        name=f"Install grader {grader_venv} : install requirements",
        commands=[
            f"{GRADER_VENV_DIR.joinpath('bin/pip3')} install -r {GRADER_REQS_FILE} --no-cache-dir --exists-action w"
        ],
    )
    server.shell(
        name=f"Install grader {grader_venv} : chown venv",
        commands=[f"/usr/bin/chown -R {grader_venv} {GRADER_VENV_DIR}"],
    )

consul_configuration = {
    Path("00-default.json"): ConsulConfig(
        addresses=ConsulAddresses(dns="127.0.0.1", http="127.0.0.1"),
        advertise_addr="{{ GetPrivateIP }}",
        services=[],
    ),
}
consul = Consul(version=VERSIONS["consul"], configuration=consul_configuration)

consul_template_configuration = {
    Path("00-graders.json"): ConsulTemplateConfig(
        vault=ConsulTemplateVaultConfig(),
        template=[
            # https://github.com/mitodl/xqueue-watcher?tab=readme-ov-file#running-xqueue-watcher
            # NOTE: The grader_config.json fetched from Vault should contain the correct
            # xqueue domain as defined in the xqueue Pulumi stack configuration.
            # The xqueue domain should be configured in Vault secret:
            # secret-xqwatcher/{DEPLOYMENT}-grader-config -> confd_json
            # Ensure the grader configuration JSON includes the correct xqueue URL from the
            # Pulumi stack (applications.xqueue.{DEPLOYMENT}.{ENVIRONMENT}) which exports
            # xqueue:domain from its stack configuration.
            ConsulTemplateTemplate(
                contents=(
                    f'{{{{- with secret "secret-xqwatcher/{DEPLOYMENT}-grader-config" -}}}}'
                    "{{ .Data.data.confd_json | toJSONPretty }}{{ end }}"
                ),
                destination=XQWATCHER_CONF_DIR.joinpath("grader_config.json"),
                user=XQWATCHER_USER,
                group=XQWATCHER_USER,
                perms="0600",
            ),
            ConsulTemplateTemplate(
                contents=(
                    f'{{{{- with secret "secret-xqwatcher/{DEPLOYMENT}-grader-config" -}}}}'
                    "{{ printf .Data.data.xqwatcher_grader_code_ssh_identity }}{{ end }}"
                ),
                destination=XQWATCHER_SSH_DIR.joinpath(
                    "xqwatcher-grader-code-ssh-identity"
                ),
                user=XQWATCHER_USER,
                group=XQWATCHER_USER,
                perms="0600",
            ),
            ConsulTemplateTemplate(
                contents=(
                    f'{{{{- with secret "secret-xqwatcher/{DEPLOYMENT}-grader-config" -}}}}'
                    "{{ .Data.data.graders_yaml | toYAML }}{{ end }}"
                ),
                destination=XQWATCHER_FETCH_GRADERS_CONFIG_FILE,
                user=XQWATCHER_USER,
                group=XQWATCHER_USER,
                perms="0600",
            ),
        ],
        restart_period="7d",
        restart_jitter="12h",
    ),
}
consul_template = ConsulTemplate(
    version=VERSIONS["consul-template"],
    configuration=consul_template_configuration,
)

vault_configuration = {
    Path("agent.json"): VaultAgentConfig(
        cache=VaultAgentCache(use_auto_auth_token="force"),  # noqa: S106
        listener=[
            VaultListener(
                tcp=VaultTCPListener(
                    address=f"127.0.0.1:{VAULT_HTTP_PORT}", tls_disable=True
                ),
            ),
        ],
        vault=VaultConnectionConfig(
            address=f"https://vault.query.consul:{VAULT_HTTP_PORT}",
            tls_skip_verify=True,
        ),
        auto_auth=VaultAutoAuthConfig(
            method=VaultAutoAuthMethod(
                type="aws",
                mount_path=f"auth/aws-{DEPLOYMENT}",
                config=VaultAutoAuthAWS(role="xqwatcher-server"),
            ),
            sink=[VaultAutoAuthSink(type="file", config=[VaultAutoAuthFileSink()])],
        ),
        restart_period="5h",
        restart_jitter="12h",
    ),
}
vault = Vault(version=VERSIONS["vault"], configuration=vault_configuration)

hashicorp_products = [consul, consul_template, vault]
install_hashicorp_products(hashicorp_products)
configure_hashicorp_products(hashicorp_products)

consul_template_permissions(consul_template.configuration)

# Install Vector
vector_config = VectorConfig(is_proxy=False)
vector_config.configuration_templates[
    TEMPLATES_DIRECTORY.joinpath("vector", "xqwatcher-logs.yaml.j2")
] = {}
install_and_configure_vector(vector_config)


if host.get_fact(HasSystemd):
    vector_service(vector_config)

    register_services(hashicorp_products, start_services_immediately=False)
    proxy_consul_dns()

    server.service(
        name="Ensure that the xqwatcher service is enabled",
        service="xqwatcher",
        running=False,
        enabled=True,
    )
    watched_xqwatcher_files = [
        XQWATCHER_LOGGING_CONFIG_FILE,
        XQWATCHER_GRADERS_CONFIG_FILE,
        XQWATCHER_FETCH_GRADERS_CONFIG_FILE,
    ]
    service_configuration_watches(
        service_name="xqwatcher.service", watched_files=watched_xqwatcher_files
    )
