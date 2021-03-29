from pydantic import BaseSettings


class OLBaseSettings(BaseSettings):
    class Config:  # noqa: WPS431
        case_sensitive = False
