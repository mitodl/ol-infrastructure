import abc
from pathlib import Path
from typing import Dict, Optional, Sequence, Tuple

from pydantic import BaseModel

from bilder.lib.model_helpers import OLBaseSettings


class HashicorpConfig(OLBaseSettings, abc.ABC):
    class Config:  # noqa: WPS431
        extra = "allow"


class FlexibleBaseModel(BaseModel):
    class Config:  # noqa: WPS431
        extra = "allow"


class HashicorpProduct(BaseModel, abc.ABC):
    name: str
    version: str
    install_directory: Optional[Path] = None
    configuration: Dict[Path, HashicorpConfig]
    configuration_directory: Optional[Path]
    configuration_file: Optional[Path]

    @abc.abstractproperty
    def systemd_template_context(self):
        raise NotImplementedError()

    @abc.abstractmethod
    def render_config_file(self) -> Sequence[Tuple[Path, str]]:
        raise NotImplementedError("This method has not been implemented")
