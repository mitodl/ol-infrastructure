from pyinfra.api import deploy
from pyinfra.operations import apt, server

from bilder.components.hashicorp.vault.models import VaultAgentConfig


@deploy("Manage Vault template destination permissions")
def vault_template_permissions(vault_config: VaultAgentConfig, state=None, host=None):
    apt.packages(
        name="Install ACL package for more granular file permissions",
        packages=["acl"],
        state=state,
        host=host,
    )
    for template in vault_config.template or []:
        filename = template.destination
        server.shell(
            commands=[
                f"setfacl -m u:vault:rwx {filename.parent}",
            ],
            state=state,
            host=host,
        )
