"""Describe an application's Vault-backed environment variables as data.

A deployed stack turns the description into VaultStaticSecret and
VaultDynamicSecret resources whose VSO templates write each variable. Local-dev
has no Vault: it builds one plain Secret whose keys are the same variable names,
with values the local program supplies. Both read the names from one
description, so a variable added for deployed stacks cannot go unnoticed
locally.
"""

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Annotated, Any, Literal

from pulumi import Output, ResourceOptions
from pydantic import BaseModel, ConfigDict, Field, model_validator

from ol_infrastructure.components.services.vault import (
    OLVaultK8SDynamicSecretConfig,
    OLVaultK8SSecret,
    OLVaultK8SSecretConfig,
    OLVaultK8SStaticSecretConfig,
)

VaultKey = Annotated[tuple[str, ...], Field(min_length=1)]
"""Keys leading to a value in the Vault secret's data: ``("value",)`` or, for a
nested value, ``("fastly", "api_key")``."""


@dataclass(frozen=True)
class Template:
    """VSO template text used as is: a value built from several keys, or a literal.

    A wrapper rather than a plain string, so a key written as ``"value"``
    instead of ``("value",)`` fails validation instead of deploying the word.
    """

    text: str


def vso_template(source: VaultKey | Template) -> str:
    """Return the VSO template text that writes one variable.

    :param source: A key path into the secret's data, or template text.
    :returns: Go template text for the destination Secret's key.
    """
    if isinstance(source, Template):
        return source.text
    if len(source) == 1:
        return f'{{{{ get .Secrets "{source[0]}" }}}}'
    keys = " ".join(f'"{key}"' for key in source)
    return f"{{{{ index .Secrets {keys} }}}}"


class VaultSecret(BaseModel):
    """One Vault path and the environment variables read from it.

    Deployed, it becomes one VaultStaticSecret or VaultDynamicSecret writing a
    Secret named ``<name>-<kind>-secret``.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    name: str
    kind: Literal["static", "dynamic"]
    mount: str | Output[str]
    mount_type: Literal["kv-v1", "kv-v2"] | None = None
    """KV engine version; required for static secrets, unset for dynamic ones."""
    path: str
    env: dict[str, VaultKey | Template]
    """Variable name to its source, in the order VSO writes them."""

    @model_validator(mode="after")
    def mount_type_matches_kind(self) -> "VaultSecret":
        if (self.kind == "static") != (self.mount_type is not None):
            msg = f"{self.name}: a static secret needs mount_type, a dynamic one none"
            raise ValueError(msg)
        return self

    @property
    def secret_name(self) -> str:
        return f"{self.name}-{self.kind}-secret"

    @property
    def templates(self) -> dict[str, str]:
        return {name: vso_template(source) for name, source in self.env.items()}


def env_names(secrets: Iterable[VaultSecret]) -> list[str]:
    """Return every variable the secrets write, in order.

    :raises ValueError: If two secrets write the same variable; which one a pod
        sees would then depend on the order of its ``envFrom`` list.
    """
    names = [name for secret in secrets for name in secret.env]
    duplicates = sorted({name for name in names if names.count(name) > 1})
    if duplicates:
        msg = f"Written by more than one secret: {', '.join(duplicates)}"
        raise ValueError(msg)
    return names


def vault_k8s_secrets(  # noqa: PLR0913
    secrets: Iterable[VaultSecret],
    *,
    resource_prefix: str,
    namespace: str,
    labels: dict[str, str],
    vaultauth: str,
    opts: ResourceOptions | None = None,
) -> list[tuple[str, OLVaultK8SSecret]]:
    """Create the VSO resources for a deployed stack.

    :param secrets: The application's description.
    :param resource_prefix: Prefix for each Pulumi resource name, e.g.
        ``ol-mitxonline-qa``.
    :param namespace: Namespace of the VSO resources and the Secrets they write.
    :param labels: Labels for both.
    :param vaultauth: The VaultAuth the resources authenticate with.
    :param opts: Resource options for every resource.
    :returns: Each written Secret's name and the resource writing it, in order.
    """
    created = []
    for secret in secrets:
        fields: dict[str, Any] = {
            "name": secret.secret_name,
            "namespace": namespace,
            "labels": labels,
            "dest_secret_name": secret.secret_name,
            "dest_secret_labels": labels,
            "mount": secret.mount,
            "path": secret.path,
            "excludes": [".*"],
            "exclude_raw": True,
            "templates": secret.templates,
            "vaultauth": vaultauth,
        }
        config: OLVaultK8SSecretConfig
        if secret.kind == "static":
            config = OLVaultK8SStaticSecretConfig.model_validate(
                fields | {"mount_type": secret.mount_type}
            )
        else:
            config = OLVaultK8SDynamicSecretConfig.model_validate(fields)
        resource = OLVaultK8SSecret(
            f"{resource_prefix}-{secret.secret_name}",
            resource_config=config,
            opts=opts,
        )
        created.append((secret.secret_name, resource))
    return created


def local_secret_data(
    names: Sequence[str],
    values: Mapping[str, Any] | None,
    unset: Iterable[Any] | None,
    supplied_elsewhere: Iterable[str] = (),
) -> dict[str, str]:
    """Return the local Secret's data for the variables a description writes.

    Every name has to be accounted for locally, so a variable added for
    deployed stacks stops the local program until someone decides on it.
    ``values`` and ``unset`` come straight from a YAML file, so an empty
    section (``None``) and unquoted values are handled here.

    :param names: The description's variable names (``env_names``).
    :param values: Local values for some of them.
    :param unset: Ones left unset locally so the application's own default
        applies.
    :param supplied_elsewhere: Ones another local Secret sets, so they stay out
        of this one.
    :returns: ``values`` in the description's order.
    :raises ValueError: If a value or unset entry is not a string, a name is in
        neither or both, or either has a name the description does not write.
    """
    values = values or {}
    unset = list(unset or [])
    non_strings = sorted(
        str(key) for key, value in values.items() if not isinstance(value, str)
    ) + sorted(repr(entry) for entry in unset if not isinstance(entry, str))
    if non_strings:
        msg = (
            "Local secret values and unset entries must be strings (quote them): "
            f"{', '.join(non_strings)}"
        )
        raise ValueError(msg)
    local = [*values, *unset, *list(supplied_elsewhere)]
    missing = [name for name in names if name not in local]
    twice = sorted({name for name in local if local.count(name) > 1})
    extra = sorted(set(local) - set(names))
    if missing or twice or extra:
        msg = (
            "Each deployed variable needs exactly one local value, unset entry "
            "or supplied_by entry; "
            f"missing: {', '.join(missing) or 'none'}; "
            f"listed twice: {', '.join(twice) or 'none'}; "
            f"not deployed: {', '.join(extra) or 'none'}"
        )
        raise ValueError(msg)
    return {name: values[name] for name in names if name in values}
