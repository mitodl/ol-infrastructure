from pydantic_settings import BaseSettings, SettingsConfigDict


class OLBaseSettings(BaseSettings):
    model_config = SettingsConfigDict(case_sensitive=False)
