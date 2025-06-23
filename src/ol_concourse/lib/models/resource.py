from pathlib import Path

from pydantic import BaseModel, ConfigDict


class Git(BaseModel):
    uri: str
    branch: str = "main"
    paths: list[Path] | None = None
    private_key: str | None = None
    ignore_paths: list[Path] | None = None
    fetch_tags: bool = False
    tag_regex: str | None = None
    depth: int | None = None
    model_config = ConfigDict(extra="allow")
