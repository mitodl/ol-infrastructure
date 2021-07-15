from pyinfra.api import deploy
from pyinfra.operations import apt, server

from bilder.components.hashicorp.consul_template.models import ConsulTemplateConfig


@deploy("Manage Vault template destination permissions")
def consul_template_permissions(
    consul_template_config: ConsulTemplateConfig, state=None, host=None
):
    apt.packages(
        name="Install ACL package for more granular file permissions",
        packages=["acl"],
        state=state,
        host=host,
    )
    for template in consul_template_config.template or []:
        filename = template.destination
        server.shell(
            commands=[
                # Recursively add read/write/execute permissions for consul-template to
                # directory
                f"setfacl -R -m u:consul-template:rwx {filename.parent}",
            ],
            state=state,
            host=host,
        )
