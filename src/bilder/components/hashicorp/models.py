import abc
from pathlib import Path
from typing import Dict, Iterable, Optional, Tuple

from pydantic import BaseModel

from bilder.lib.model_helpers import OLBaseSettings


class HashicorpConfig(OLBaseSettings, abc.ABC):
    class Config:  # noqa: WPS431
        extra = "allow"


class FlexibleBaseModel(BaseModel):
    class Config:  # noqa: WPS431
        extra = "allow"


class HashicorpProduct(BaseModel, abc.ABC):
    _name: str
    version: str
    install_directory: Optional[Path] = None
    configuration: Dict[Path, HashicorpConfig]
    configuration_directory: Optional[Path]
    configuration_file: Optional[Path]

    @abc.abstractproperty
    def systemd_template_context(self):
        raise NotImplementedError()

    @abc.abstractmethod
    def render_configuration_files(self) -> Iterable[Tuple[Path, str]]:
        raise NotImplementedError("This method has not been implemented")

    @property
    def name(self):
        return self._name
