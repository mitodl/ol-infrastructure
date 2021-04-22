from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

from bilder.components.hashicorp.models import (
    FlexibleBaseModel,
    HashicorpConfig,
    HashicorpProduct,
)


class ConsulTemplateTemplate(FlexibleBaseModel):
    # This is the source file on disk to use as the input template. This is often
    # called the "Consul Template template". This option is required if not using
    # the `contents` option.
    source: Optional[Path]
    # This is the destination path on disk where the source template will render.
    # If the parent directories do not exist, Consul Template will attempt to
    # create them, unless create_dest_dirs is false.
    destination: Path
    # This options tells Consul Template to create the parent directories of the
    # destination path if they do not exist. The default value is true.
    create_dest_dirs: bool = True
    # This option allows embedding the contents of a template in the configuration
    # file rather then supplying the `source` path to the template file. This is
    # useful for short templates. This option is mutually exclusive with the
    # `source` option.
    contents: Optional[str]  # noqa: WPS110
    # This is the optional command to run when the template is rendered. The
    # command will only run if the resulting template changes. The command must
    # return within 30s (configurable), and it must have a successful exit code.
    # Consul Template is not a replacement for a process monitor or init system.
    # Please see the [Command](#command) section below for more.
    command: Optional[str]
    # This is the maximum amount of time to wait for the optional command to
    # return. If you set the timeout to 0s the command is run in the background
    # without monitoring it for errors. If also using Once, consul-template can
    # exit before the command is finished. Default is 30s.
    command_timeout: Optional[str]
    # Exit with an error when accessing a struct or map field/key that does not
    # exist. The default behavior will print "<no value>" when accessing a field
    # that does not exist. It is highly recommended you set this to "true" when
    # retrieving secrets from Vault.
    error_on_missing_key: bool = False
    # This is the permission to render the file. If this option is left
    # unspecified, Consul Template will attempt to match the permissions of the
    # file that already exists at the destination path. If no file exists at that
    # path, the permissions are 0644.
    perms: Optional[str]
    # This option backs up the previously rendered template at the destination
    # path before writing a new one. It keeps exactly one backup. This option is
    # useful for preventing accidental changes to the data without having a
    # rollback strategy.
    backup: bool = True
    # These are the delimiters to use in the template. The default is "{{" and
    # "}}", but for some templates, it may be easier to use a different delimiter
    # that does not conflict with the output file itself.
    left_delimiter: str = "{{"
    right_delimiter: str = "}}"
    # These are functions that are not permitted in the template. If a template
    # includes one of these functions, it will exit with an error.
    function_blacklist: Optional[List[str]] = []
    # If a sandbox path is provided, any path provided to the `file` function is
    # checked that it falls within the sandbox path. Relative paths that try to
    # traverse outside the sandbox path will exit with an error.
    sandbox_path: Optional[Path]
    # This is the `minimum(:maximum)` to wait before rendering a new template to
    # disk and triggering a command, separated by a colon (`:`). If the optional
    # maximum value is omitted, it is assumed to be 4x the required minimum value.
    # This is a numeric time with a unit suffix ("5s"). There is no default value.
    # The wait value for a template takes precedence over any globally-configured
    # wait.
    wait: Optional[str]


class ConsulTemplateConfig(HashicorpConfig):
    template: List[ConsulTemplateTemplate] = [
        ConsulTemplateTemplate(destination=Path("/tmp/test.txt"))  # noqa: S108
    ]
    vault_agent_token_file: Optional[Path]

    class Config:  # noqa: WPS431
        env_prefix = "consul_template_"


class ConsulTemplate(HashicorpProduct):
    _name: str = "consul-template"
    version: str = "0.25.2"
    configuration: Dict[Path, ConsulTemplateConfig] = {
        Path("/etc/consul-template.d/00-default.json"): ConsulTemplateConfig()
    }
    configuration_directory: Path = Path("/etc/consul-template.d/")

    @property
    def systemd_template_context(self):
        return self

    def render_configuration_files(self) -> Iterable[Tuple[Path, str]]:
        for fpath, config in self.configuration.items():  # noqa: WPS526
            yield fpath, config.json(exclude_none=True, indent=2)
