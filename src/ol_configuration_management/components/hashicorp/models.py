from typing import List

from pydantic import BaseModel

from ol_configuration_management.lib.model_helpers import OLBaseSettings


class HashicorpProduct(BaseModel):
    name: str
    version: str


class HashicorpConfig(OLBaseSettings):
    products: List[HashicorpProduct]

    class Config:  # noqa: WPS431
        env_prefix = "hashicorp_"


class ConsulConfig(OLBaseSettings):
    class Config:  # noqa: WPS431
        env_prefix = "consul_"


class ConsulTemplateConfig(OLBaseSettings):
    class Config:  # noqa: WPS431
        env_prefix = "consul_template"


class NomadConfig(OLBaseSettings):
    class Config:  # noqa: WPS431
        env_prefix = "nomad_"


class VaultConfig(OLBaseSettings):
    class Config:  # noqa: WPS431
        env_prefix = "vault_"
