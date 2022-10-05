from ol_configuration_management.lib.model_helpers import OLBaseSettings


class TraefikConfig(OLBaseSettings):
    class Config:
        env_prefix = "traefik_"
