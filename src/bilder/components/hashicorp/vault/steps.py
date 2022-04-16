from pyinfra.api import deploy
from pyinfra.operations import apt, server

from bilder.components.hashicorp.vault.models import VaultAgentConfig


@deploy("Manage Vault template destination permissions")
def vault_template_permissions(vault_config: VaultAgentConfig):
    apt.packages(
        name="Install ACL package for more granular file permissions",
        packages=["acl"],
    )
    for template in vault_config.template or []:
        filename = template.destination
        server.shell(
            commands=[
                # Recursively add read/write permissions for Vault to directory
                f"setfacl -R -m u:vault:rwx {filename.parent}",
                f"setfacl -R -d -m u:vault:rwx {filename.parent}",
            ],
        )
