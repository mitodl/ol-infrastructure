from pathlib import Path
from typing import Optional, Sequence, Tuple

from pydantic import BaseModel

from bilder.lib.model_helpers import OLBaseSettings


class HashicorpProduct(BaseModel):
    name: str
    version: str
    install_directory: Optional[Path] = None


class HashicorpConfig(OLBaseSettings):
    class Config:  # noqa: WPS431
        extra = "allow"

    def render_config_files(self) -> Sequence[Tuple[Path, str]]:
        raise NotImplementedError("This method has not been implemented")


class FlexibleBaseModel(BaseModel):
    class Config:  # noqa: WPS431
        extra = "allow"
