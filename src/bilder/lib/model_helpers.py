import re
from typing import Any

from pydantic_settings import BaseSettings, SettingsConfigDict


class OLBaseSettings(BaseSettings):
    model_config = SettingsConfigDict(case_sensitive=False)


def parse_simple_duration_string(dur: str) -> re.Match[Any] | None:
    return re.match(r"^([1-9])\d*(m|h|d)$", dur)
