from pathlib import Path
from typing import Dict

from pyinfra.api import deploy
from pyinfra.operations import apt, server

from bilder.components.hashicorp.consul_template.models import ConsulTemplateConfig


@deploy("Manage consul-template template destination permissions")
def consul_template_permissions(
    consul_template_configs: Dict[Path, ConsulTemplateConfig], state=None, host=None
):
    apt.packages(
        name="Install ACL package for more granular file permissions",
        packages=["acl"],
        state=state,
        host=host,
    )
    templates = []
    for config in consul_template_configs.values():
        templates.extend(config.template or [])
    for template in templates:
        filename = template.destination
        server.shell(
            commands=[
                # Recursively add read/write permissions for consul-template to
                # directory
                f"setfacl -R -m u:consul-template:rwx {filename.parent}",
                f"setfacl -R -d -m u:consul-template:rwx {filename.parent}",
            ],
            state=state,
            host=host,
        )
