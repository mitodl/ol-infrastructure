import abc
from collections.abc import Iterable
from pathlib import Path
from typing import Optional

from pydantic import ConfigDict, BaseModel

from bilder.lib.model_helpers import OLBaseSettings


class HashicorpConfig(OLBaseSettings, abc.ABC):
    model_config = ConfigDict(extra="allow")


class FlexibleBaseModel(BaseModel):
    model_config = ConfigDict(extra="allow")


class HashicorpProduct(BaseModel, abc.ABC):
    _name: str
    version: str
    install_directory: Optional[Path] = None
    configuration: dict[Path, HashicorpConfig]
    configuration_directory: Optional[Path] = None
    configuration_file: Optional[Path] = None

    @abc.abstractproperty
    def systemd_template_context(self):
        raise NotImplementedError()

    @abc.abstractmethod
    def render_configuration_files(self) -> Iterable[tuple[Path, str]]:
        raise NotImplementedError("This method has not been implemented")

    @property
    def name(self):
        return self._name
