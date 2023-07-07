from pathlib import Path
from typing import Optional

from pydantic import ConfigDict, BaseModel


class Git(BaseModel):
    uri: str
    branch: str = "main"
    paths: Optional[list[Path]] = None
    private_key: Optional[str] = None
    ignore_paths: Optional[list[Path]] = None
    model_config = ConfigDict(extra="allow")
