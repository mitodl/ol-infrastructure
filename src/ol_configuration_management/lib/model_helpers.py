from pydantic import BaseSettings


class OLBaseSettings(BaseSettings):
    class Config:
        case_sensitive = False
