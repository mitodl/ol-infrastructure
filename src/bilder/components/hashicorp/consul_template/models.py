from collections.abc import Iterable
from pathlib import Path
from typing import Self

from pydantic import field_validator, model_validator
from pydantic_settings import SettingsConfigDict

from bilder.components.hashicorp.models import (
    FlexibleBaseModel,
    HashicorpConfig,
    HashicorpProduct,
)
from bilder.lib.model_helpers import parse_simple_duration_string


class ConsulTemplateVaultConfig(FlexibleBaseModel):
    # This is the address of the Vault leader. The protocol (http(s)) portion
    # of the address is required.
    address: str = "http://localhost:8200"

    # This is a Vault Enterprise namespace to use for reading/writing secrets.
    #
    # This value can also be specified via the environment variable VAULT_NAMESPACE.
    namespace: str | None = None

    # This is the token to use when communicating with the Vault server.
    # Like other tools that integrate with Vault, Consul Template makes the
    # assumption that you provide it with a Vault token; it does not have the
    # incorporated logic to generate tokens via Vault's auth methods.
    #
    # This value can also be specified via the environment variable VAULT_TOKEN.
    # It is highly recommended that you do not put your token in plain-text in a
    # configuration file.
    #
    # When using a token from Vault Agent, the vault_agent_token_file setting
    # should be used instead, as that will take precedence over this field.
    token: str | None = None

    # This tells Consul Template to load the Vault token from the contents of a file.
    # If this field is specified:
    # - by default Consul Template will not try to renew the Vault token, if you want it
    # to renew you will need to specify renew_token = true as below.
    # - Consul Template will periodically stat the file and update the token if it has
    # changed.

    # This tells Consul Template that the provided token is actually a wrapped
    # token that should be unwrapped using Vault's cubbyhole response wrapping
    # before being used. Please see Vault's cubbyhole response wrapping
    # documentation for more information.
    unwrap_token: bool | None = None

    # The default lease duration Consul Template will use on a Vault secret that
    # does not have a lease duration. This is used to calculate the sleep duration
    # for rechecking a Vault secret value. This field is optional and will default to
    # 5 minutes.
    default_lease_duration: str | None = None

    # This option tells Consul Template to automatically renew the Vault token
    # given. If you are unfamiliar with Vault's architecture, Vault requires
    # tokens be renewed at some regular interval or they will be revoked. Consul
    # Template will automatically renew the token at half the lease duration of
    # the token. The default value is true, but this option can be disabled if
    # you want to renew the Vault token using an out-of-band process.
    #
    # Note that secrets specified in a template (using {{secret}} for example)
    # are always renewed, even if this option is set to false. This option only
    # applies to the top-level Vault token itself.
    renew_token: bool | None = None

    # This section details the retry options for connecting to Vault. Please see
    # the retry options in the Consul section for more information (they are the
    # same).
    retry: dict[str, bool | int | str] | None = None

    # This section details the SSL options for connecting to the Vault server.
    # Please see the SSL options in the Consul section for more information (they
    # are the same).
    ssl: dict[str, bool | str] | None = None


class ConsulTemplateConsulConfig(FlexibleBaseModel):
    # This block specifies the basic authentication information to pass with the
    # request. For more information on authentication, please see the Consul
    # documentation.
    auth: dict[str, bool | str] | None = None

    # This is the address of the Consul agent. By default, this is
    # 127.0.0.1:8500, which is the default bind and port for a local Consul
    # agent. It is not recommended that you communicate directly with a Consul
    # server, and instead communicate with the local Consul agent. There are many
    # reasons for this, most importantly the Consul agent is able to multiplex
    # connections to the Consul server and reduce the number of open HTTP
    # connections. Additionally, it provides a "well-known" IP address for which
    # clients can connect.
    address: str = "127.0.0.1:8500"

    # This is a Consul Enterprise namespace to use for reading/writing. This can
    # also be set via the CONSUL_NAMESPACE environment variable.
    # BETA: this is to be considered a beta feature as it has had limited testing
    namespace: str | None = None

    # This is the ACL token to use when connecting to Consul. If you did not
    # enable ACLs on your Consul cluster, you do not need to set this option.
    #
    # This option is also available via the environment variable CONSUL_TOKEN.
    # It is highly recommended that you do not put your token in plain-text in a
    # configuration file.
    token: str | None = None

    # This controls the retry behavior when an error is returned from Consul.
    # Consul Template is highly fault tolerant, meaning it does not exit in the
    # face of failure. Instead, it uses exponential back-off and retry functions
    # to wait for the cluster to become available, as is customary in distributed
    # systems.
    retry: dict[str, bool | int | str] | None = None
    # This enabled retries. Retries are enabled by default, so this is
    # redundant.

    # This specifies the number of attempts to make before giving up. Each
    # attempt adds the exponential backoff sleep time. Setting this to
    # zero will implement an unlimited number of retries.

    # This is the base amount of time to sleep between retry attempts. Each
    # retry sleeps for an exponent of 2 longer than this base. For 5 retries,
    # the sleep times would be: 250ms, 500ms, 1s, 2s, then 4s.

    # This is the maximum amount of time to sleep between retry attempts.
    # When max_backoff is set to zero, there is no upper limit to the
    # exponential sleep between retry attempts.
    # If max_backoff is set to 10s and backoff is set to 1s, sleep times
    # would be: 1s, 2s, 4s, 8s, 10s, 10s, ...

    # This block configures the SSL options for connecting to the Consul server.
    ssl: dict[str, bool | str] | None = None
    # This enables SSL. Specifying any option for SSL will also enable it.

    # This enables SSL peer verification. The default value is "true", which
    # will check the global CA chain to make sure the given certificates are
    # valid. If you are using a self-signed certificate that you have not added
    # to the CA chain, you may want to disable SSL verification. However, please
    # understand this is a potential security vulnerability.

    # This is the path to the certificate to use to authenticate. If just a
    # certificate is provided, it is assumed to contain both the certificate and
    # the key to convert to an X509 certificate. If both the certificate and
    # key are specified, Consul Template will automatically combine them into an
    # X509 certificate for you.

    # This is the path to the certificate authority to use as a CA. This is
    # useful for self-signed certificates or for organizations using their own
    # internal certificate authority.

    # This is the path to a directory of PEM-encoded CA cert files. If both
    # `ca_cert` and `ca_path` is specified, `ca_cert` is preferred.

    # This sets the SNI server name to use for validation.


class ConsulTemplateTemplate(FlexibleBaseModel):
    # This is the source file on disk to use as the input template. This is often
    # called the "Consul Template template". This option is required if not using
    # the `contents` option.
    source: Path | None = None
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
    contents: str | None = None
    # This is the optional command to run when the template is rendered. The
    # command will only run if the resulting template changes. The command must
    # return within 30s (configurable), and it must have a successful exit code.
    # Consul Template is not a replacement for a process monitor or init system.
    # Please see the [Command](#command) section below for more.
    command: str | None = None
    # This is the maximum amount of time to wait for the optional command to
    # return. If you set the timeout to 0s the command is run in the background
    # without monitoring it for errors. If also using Once, consul-template can
    # exit before the command is finished. Default is 30s.
    command_timeout: str | None = None
    # Exit with an error when accessing a struct or map field/key that does not
    # exist. The default behavior will print "<no value>" when accessing a field
    # that does not exist. It is highly recommended you set this to "true" when
    # retrieving secrets from Vault.
    error_on_missing_key: bool = False
    # This is the permission to render the file. If this option is left
    # unspecified, Consul Template will attempt to match the permissions of the
    # file that already exists at the destination path. If no file exists at that
    # path, the permissions are 0644.
    perms: str | None = None
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
    function_blacklist: list[str] | None = []  # noqa: RUF012
    # If a sandbox path is provided, any path provided to the `file` function is
    # checked that it falls within the sandbox path. Relative paths that try to
    # traverse outside the sandbox path will exit with an error.
    sandbox_path: Path | None = None
    # This is the `minimum(:maximum)` to wait before rendering a new template to
    # disk and triggering a command, separated by a colon (`:`). If the optional
    # maximum value is omitted, it is assumed to be 4x the required minimum value.
    # This is a numeric time with a unit suffix ("5s"). There is no default value.
    # The wait value for a template takes precedence over any globally-configured
    # wait.
    wait: str | None = None
    # These are the user and group ownerships of the rendered file. They can be
    # specified in the form of username/group name or UID/GID. If left unspecified,
    # Consul Template will preserve the ownerships of the existing file. If no file
    # exists, the ownerships will default to the user running Consul Template. This
    # option is not supported on Windows.
    user: str | int | None = None
    group: str | int | None = None


class ConsulTemplateConfig(HashicorpConfig):
    model_config = SettingsConfigDict(env_prefix="consul_template_")
    template: list[ConsulTemplateTemplate] = [  # noqa: RUF012
        ConsulTemplateTemplate(destination=Path("/tmp/test.txt"))  # noqa: S108
    ]
    vault_agent_token_file: Path | None = None
    vault: ConsulTemplateVaultConfig | None = None
    consul: ConsulTemplateConsulConfig | None = ConsulTemplateConsulConfig()
    restart_period: str | None = None
    restart_jitter: str | None = None

    @model_validator(mode="after")
    def validate_restart_settings(self) -> Self:
        if self.restart_period is not None and self.restart_jitter is None:
            msg = "If restart_period is set then a restart_jitter must be supplied."
            raise ValueError(msg)
        return self

    @field_validator("restart_period")
    @classmethod
    def validate_restart_period(cls, restart_period) -> str:
        if restart_period and not parse_simple_duration_string(restart_period):
            msg = f"Invalid restart_period duration: {restart_period}"
            raise ValueError(msg)
        return restart_period

    @field_validator("restart_jitter")
    @classmethod
    def validate_restart_jitter(cls, restart_jitter) -> str:
        if restart_jitter and not parse_simple_duration_string(restart_jitter):
            msg = f"Invalid restart_jitter duration: {restart_jitter}"
            raise ValueError(msg)
        return restart_jitter


class ConsulTemplate(HashicorpProduct):
    _name: str = "consul-template"
    version: str = "0.26.0"
    configuration: dict[Path, ConsulTemplateConfig] = {  # noqa: RUF012
        Path("00-default.json"): ConsulTemplateConfig()
    }
    configuration_directory: Path = Path("/etc/consul-template/conf.d/")

    @property
    def systemd_template_context(self):
        conf_path = next(iter(self.configuration.keys()))
        return {
            "configuration_directory": self.configuration_directory,
            "restart_period": self.configuration[conf_path].restart_period,
            "restart_jitter": self.configuration[conf_path].restart_jitter,
        }

    def render_configuration_files(self) -> Iterable[tuple[Path, str]]:
        for fpath, config in self.configuration.items():
            yield (
                self.configuration_directory.joinpath(fpath),
                config.model_dump_json(
                    exclude_none=True,
                    exclude={"restart_period", "restart_jitter"},
                    indent=2,
                ),
            )
