from ol_configuration_management.lib.model_helpers import OLBaseSettings


class NomadConfig(OLBaseSettings):
    class Config:  # noqa: WPS431
        env_prefix = "nomad_"


class NomadJob(OLBaseSettings):
    class Config:  # noqa: WPS431
        env_prefix = "nomad_job_"
