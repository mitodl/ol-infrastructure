from ol_configuration_management.lib.model_helpers import OLBaseSettings
from pydantic_settings import SettingsConfigDict


class SupersetConfig(OLBaseSettings):
    model_config = SettingsConfigDict(env_prefix="superset_")
