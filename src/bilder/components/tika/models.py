from pathlib import Path

from pydantic_settings import SettingsConfigDict

from bilder.lib.model_helpers import OLBaseSettings


class TikaConfig(OLBaseSettings):
    model_config = SettingsConfigDict(env_prefix="tika_")
    version: str = "2.4.0"
    install_directory: Path = Path("/opt/tika")
    tika_user: str = "tika"

    download_url: str = (
        f"https://archive.apache.org/dist/tika/{version}/tika-server-standard-{version}.jar"
    )
    heap_max: str = "2048"
    heap_min: str = "1024"
    install_path: Path = Path(f"/opt/tika/tika-server.{version}.jar")
    tika_host: str = "localhost"
    tika_config_file: str = f"{install_directory}/tika-config.xml"
    tika_group: str = tika_user
    tika_log_config_file: str = f"{install_directory}/log4j2_tika.xml"
    tika_port: str = "9998"
