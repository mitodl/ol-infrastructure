from pathlib import Path
from typing import Optional

from pydantic import BaseModel, ConfigDict


class Git(BaseModel):
    uri: str
    branch: str = "main"
    paths: Optional[list[Path]] = None
    private_key: Optional[str] = None
    ignore_paths: Optional[list[Path]] = None
    fetch_tags: bool = False
    tag_regex: Optional[str] = None
    depth: Optional[int] = None
    model_config = ConfigDict(extra="allow")
