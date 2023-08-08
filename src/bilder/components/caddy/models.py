from itertools import chain
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlencode, urlsplit, urlunsplit

from pydantic.main import BaseModel
from pydantic_settings import SettingsConfigDict

from bilder.lib.model_helpers import OLBaseSettings


class CaddyPlugin(BaseModel):
    """Manage installation of plugins for Caddy.

    The complete list of available plugins can be reviewed at
    https://caddyserver.com/download
    """

    repository: str
    version: Optional[str] = None


class CaddyConfig(OLBaseSettings):
    model_config = SettingsConfigDict(env_prefix="caddy_")
    caddyfile: Path = Path(__file__).resolve().parent.joinpath("templates/Caddyfile.j2")
    data_directory: Path = Path("/var/lib/caddy/")
    domains: Optional[list[str]] = None
    log_file: Optional[Path] = Path("/var/log/caddy/caddy.log")
    plugins: Optional[list[CaddyPlugin]] = None
    template_context: Optional[dict[str, Any]] = None
    upstream_address: Optional[str] = None
    tls_cert_path: Path = Path("/etc/caddy/odl_wildcard.cert")
    tls_key_path: Path = Path("/etc/caddy/odl_wildcard.key")
    caddy_user: Optional[str] = "caddy"

    def custom_download_url(self, os: str = "linux", arch: str = "amd64"):
        url_base = "https://caddyserver.com/api/download"
        url_parameters = {
            "os": os,
            "arch": arch,
            "p": [
                "@".join((plugin.repository, plugin.version or "")).strip("@")
                for plugin in self.plugins or []
            ],
        }
        return urlunsplit(
            list(
                chain(
                    urlsplit(url_base)[:3], (urlencode(url_parameters, doseq=True), "")
                )
            )
        )
