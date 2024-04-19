# ruff: noqa: E501

import os
from pathlib import Path

from bridge.lib.magic_numbers import VAULT_HTTP_PORT
from bridge.lib.versions import CONSUL_TEMPLATE_VERSION, CONSUL_VERSION, VAULT_VERSION
from bridge.secrets.sops import set_env_secrets
from pyinfra import host
from pyinfra.operations import apt, files, git, server

from bilder.components.baseline.steps import install_baseline_packages
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
from bilder.facts.has_systemd import HasSystemd

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

XQWATCHER_HOME = Path("/home/xqwatcher")
XQWATCHER_INSTALL_DIR = XQWATCHER_HOME.joinpath("xqwatcher")
XQWATCHER_VENV_DIR = XQWATCHER_INSTALL_DIR.joinpath(".venv")
XQWATCHER_CONF_DIR = XQWATCHER_INSTALL_DIR.joinpath("conf.d")
XQWATCHER_LOG_DIR = XQWATCHER_INSTALL_DIR.joinpath("log")
XQWATCHER_GRADER_DIR = XQWATCHER_INSTALL_DIR.joinpath("graders")
XQWATCHER_GRADER_VENVS_DIR = XQWATCHER_INSTALL_DIR.joinpath("grader_venvs")
XQWATCHER_GRADER_CONFIG_FILE = XQWATCHER_INSTALL_DIR.joinpath("grader_config.yaml")
XQWATCHER_LOGGING_CONFIG_FILE = XQWATCHER_INSTALL_DIR.joinpath("logging.json")
XQWATCHER_SSH_DIR = XQWATCHER_HOME.joinpath(".ssh")
XQWATCHER_BRANCH = "master"
XQWATCHER_GIT_REPO = "https://github.com/mitodl/xqueue-watcher.git"
XQWATCHER_USER = "xqwatcher"

shared_template_context = {
    "XQWATCHER_LOG_DIR": str(XQWATCHER_LOG_DIR),
    "XQWATCHER_GRADER_VENVS_DIR": str(XQWATCHER_GRADER_VENVS_DIR),
    "XQWATCHER_GRADER_DIR": str(XQWATCHER_GRADER_DIR),
    "XQWATCHER_GRADER_CONFIG_FILE": str(XQWATCHER_GRADER_CONFIG_FILE),
}

server.user(
    name="Create xqwatcher user and home directory",
    create_home=True,
    ensure_home=True,
    home=str(XQWATCHER_HOME),
    present=True,
    shell="/bin/bash",  # noqa: S604
    user=XQWATCHER_USER,
)

git.repo(
    name="Clone xqwatcher repository from github",
    branch=XQWATCHER_BRANCH,
    dest=str(XQWATCHER_INSTALL_DIR),
    group="xqwatcher",
    pull=True,
    src=XQWATCHER_GIT_REPO,
    user="xqwatcher",
)

server.shell(
    name="Create virtual environment for xqwatcher",
    commands=[f"/usr/bin/virtualenv {XQWATCHER_VENV_DIR}"],
)

files.put(
    name="Place custom xqwatcher requirements file.",
    src=str(FILES_DIRECTORY.joinpath("requirements.txt")),
    dest=str(XQWATCHER_INSTALL_DIR.joinpath("requirements.txt")),
    mode="0664",
    user="xqwatcher",
    group="xqwatcher",
)

server.shell(
    name="Install xqwatcher requirements into the virtual environment",
    commands=[
        f"{XQWATCHER_VENV_DIR.joinpath('bin/pip3')} install -r {XQWATCHER_INSTALL_DIR.joinpath('requirements.txt')} --no-cache-dir --exists-action w"
    ],
)
files.directory(
    name="Remove the existing conf.d directory for xqwatcher configurations",
    path=str(XQWATCHER_CONF_DIR),
    present=False,
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
    name="Create grader fetch script",
    src=TEMPLATES_DIRECTORY.joinpath("fetch_graders.py.j2"),
    dest=str(XQWATCHER_INSTALL_DIR.joinpath("fetch_graders.py")),
    user=XQWATCHER_USER,
    group=XQWATCHER_USER,
    mode="0754",
    shared_context=shared_template_context,
)

files.directory(
    name="Create grader directory",
    path=str(XQWATCHER_GRADER_DIR),
    user=XQWATCHER_USER,
    group=XQWATCHER_USER,
    mode="0750",
)

files.directory(
    name="Create grader venvs directory",
    path=str(XQWATCHER_GRADER_VENVS_DIR),
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

# Grader virtual environment setup
grader_venvs = ["mit-600x", "mit-686x-mooc", "mit-686x", "mit-6S082", "mit-940"]
for grader_venv in grader_venvs:
    GRADER_VENV_DIR = XQWATCHER_GRADER_VENVS_DIR.joinpath(grader_venv)
    GRADER_REQS_FILE = GRADER_VENV_DIR.joinpath("requirements.txt")
    server.shell(
        name=f"Install grader {grader_venv} : Create virtual environment",
        commands=[f"/usr/bin/virtualenv {GRADER_VENV_DIR}"],
    )
    files.put(
        name=f"Install grader {grader_venv} : create requirements file",
        src=str(FILES_DIRECTORY.joinpath(f"grader_reqs/{grader_venv}.txt")),
        dest=str(GRADER_REQS_FILE),
        mode="0664",
        user="xqwatcher",
        group="xqwatcher",
    )
    server.shell(
        name=f"Install grader {grader_venv} : install requirements",
        commands=[
            f"{GRADER_VENV_DIR.joinpath('bin/pip3')} install -r {GRADER_REQS_FILE} --no-cache-dir --exists-action w"
        ],
    )

consul_configuration = {
    Path("00-default.json"): ConsulConfig(
        addresses=ConsulAddresses(dns="127.0.0.1", http="127.0.0.1"),
        advertise_addr='{{ GetInterfaceIP "ens5" }}',
        services=[],
    ),
}
consul = Consul(version=VERSIONS["consul"], configuration=consul_configuration)

consul_template_configuration = {
    Path("00-graders.json"): ConsulTemplateConfig(
        vault=ConsulTemplateVaultConfig(),
        template=[
            # https://github.com/mitodl/xqueue-watcher?tab=readme-ov-file#running-xqueue-watcher
            ConsulTemplateTemplate(
                contents=(
                    '{{- with secret "secret-xqwatcher/grader-config" -}}'
                    "{{ .Data.data.confd_json | toJSONPretty }}{{ end }}"
                ),
                destination=XQWATCHER_CONF_DIR.joinpath("grader_config.json"),
                user=XQWATCHER_USER,
                group=XQWATCHER_USER,
                perms="0600",
            ),
            ConsulTemplateTemplate(
                contents=(
                    '{{- with secret "secret-xqwatcher/grader-config" -}}'
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
                    '{{ with secret "secret-xqwatcher/grader-config" }}'
                    "{{ .Data.data.graders_yaml | toYAML }}{{ end }}"
                ),
                destination=XQWATCHER_GRADER_CONFIG_FILE,
                user=XQWATCHER_USER,
                group=XQWATCHER_USER,
                perms="0600",
            ),
        ],
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
                mount_path="auth/aws",
                config=VaultAutoAuthAWS(role="xqwatcher-server"),
            ),
            sink=[VaultAutoAuthSink(type="file", config=[VaultAutoAuthFileSink()])],
        ),
    ),
}
vault = Vault(version=VERSIONS["vault"], configuration=vault_configuration)

hashicorp_products = [consul, consul_template, vault]
install_hashicorp_products(hashicorp_products)
configure_hashicorp_products(hashicorp_products)

consul_template_permissions(consul_template.configuration)

if host.get_fact(HasSystemd):
    register_services(hashicorp_products, start_services_immediately=False)
    proxy_consul_dns()
