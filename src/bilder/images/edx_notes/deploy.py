import os
from pathlib import Path

from pyinfra import host
from pyinfra.operations.server import files

from bilder.components.hashicorp.consul_template.models import (
    ConsulTemplateConfig,
    ConsulTemplateTemplate,
)
from bilder.components.hashicorp.consul_template.steps import (
    consul_template_permissions,
)
from bilder.components.hashicorp.steps import (
    configure_hashicorp_product,
    register_services,
)
from bilder.components.hashicorp.vault.models import (
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
from bilder.components.traefik.models import traefik_static
from bilder.components.traefik.models.component import TraefikConfig
from bilder.components.traefik.steps import configure_traefik
from bilder.facts.has_systemd import HasSystemd
from bilder.lib.linux_helpers import DOCKER_COMPOSE_DIRECTORY
from bridge.lib.magic_numbers import VAULT_HTTP_PORT

TEMPLATES_DIRECTORY = Path(__file__).resolve().parent.joinpath("templates")
FILES_DIRECTORY = Path(__file__).resolve().parent.joinpath("files")
CONSUL_TEMPLATE_DIRECTORY = Path("/etc/consul-template/")
DEPLOYMENT = os.environ.get("DEPLOYMENT")

traefik_static_config = traefik_static.TraefikStaticConfig(
    log=traefik_static.Log(format="json"),
    providers=traefik_static.Providers(docker=traefik_static.Docker()),
    certificates_resolvers={
        "letsencrypt_resolver": traefik_static.CertificatesResolvers(
            acme=traefik_static.Acme(
                email="odl-devops@mit.edu",
                storage="/etc/traefik/acme.json",
                dns_challenge=traefik_static.DnsChallenge(provider="route53"),
            )
        )
    },
    entry_points={
        "http": traefik_static.EntryPoints(
            address=":80",
            http=traefik_static.Http(
                redirectrions=traefik_static.Redirections(
                    entry_point=traefik_static.EntryPoint(
                        to="https",
                        scheme="https",
                        permanent=True,
                    )
                )
            ),
        ),
        "https": traefik_static.EntryPoints(address=":443"),
    },
)
traefik_config = TraefikConfig(static_configuration=traefik_static_config)

configure_traefik(traefik_config)

consul_template = ConsulTemplateConfig(
    template=[
        ConsulTemplateTemplate(
            source=CONSUL_TEMPLATE_DIRECTORY.joinpath("env.tmpl"),
            destination=DOCKER_COMPOSE_DIRECTORY.joinpath(".env"),
        )
    ]
)

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
            mount_path=f"auth/aws-{DEPLOYMENT}",
            config=VaultAutoAuthAWS(role="edx-notes-server"),
        ),
        sink=[VaultAutoAuthSink(type="file", config=[VaultAutoAuthFileSink()])],
    ),
)

files.put(
    name="Upload docker compose file",
    src=str(FILES_DIRECTORY.joinpath("docker-compose.yaml")),
    dest=str(CONSUL_TEMPLATE_DIRECTORY.joinpath("docker-compose.yaml.tmpl")),
    mode="0664",
)

files.put(
    name="Upload env file for docker-compose",
    src=str(FILES_DIRECTORY.joinpath("env.tmpl")),
    dest=str(CONSUL_TEMPLATE_DIRECTORY.joinpath(".env.tmpl")),
    mode="0664",
)

files.put(
    name="Upload env file for docker-compose",
    src=str(FILES_DIRECTORY.joinpath("docker-compose.yaml")),
    dest=str(DOCKER_COMPOSE_DIRECTORY.joinpath("docker-compose.yaml")),
    mode="0664",
)

configure_hashicorp_product(vault_config)
configure_hashicorp_product(consul_template)
consul_template_permissions(consul_template.configuration)
if host.get_fact(HasSystemd):
    register_services((vault_config, consul_template), start_services_immediately=False)
