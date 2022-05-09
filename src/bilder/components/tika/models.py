from pathlib import Path
from typing import Any, Dict, Optional

from bilder.lib.model_helpers import OLBaseSettings


class TikaConfig(OLBaseSettings):
    version: str = "2.4.0"
    install_directory: Path = Path("/opt/tika")
    install_path: Path = Path(f"/opt/tika/tika-server.{version}.jar")
    log4j_config_file: Path = (
        Path(__file__).resolve().parent.joinpath("files/log4j_tika.xml")
    )
    download_url: str = f"https://archive.apache.org/dist/tika/{version}/tika-server-standard-{version}.jar"
    tika_user: str = "tika"

    template_context: Optional[Dict[str, Any]] = {
        "heap_max": "2048",
        "heap_min": "1024",
        "tika_config_file": "/etc/tika/tika-config.xml",
        "tika_group": tika_user,
        "tika_host": "localhost",
        "tika_log_config_file": "/etc/tika/log4j_tika.xml",
        "tika_path": str(install_directory),
        "tika_port": "9998",
        "tika_user": tika_user,
        "tika_version": version,
    }

    class Config:
        env_prefix = "tika_"
