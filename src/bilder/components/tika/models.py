from pathlib import Path
from typing import Any, Dict, Optional

from bilder.lib.model_helpers import OLBaseSettings


class TikaConfig(OLBaseSettings):
    version: str = "1.23"
    install_directory: Path = Path("/opt/tika")
    install_path: Path = Path(f"/opt/tika/tika-server.{version}.jar")
    log4j_config_file: Path = (
        Path(__file__).resolve().parent.joinpath("files/log4j_tika.xml")
    )
    download_url: str = (
        f"https://archive.apache.org/dist/tika/tika-server-{version}.jar"
    )
    tika_user: str = "tika"

    template_context: Optional[Dict[str, Any]] = {
        "tika_user": tika_user,
        "tika_group": tika_user,
        "tika_host": "0.0.0.0",
        "tika_port": "9998",
        "tika_path": str(install_directory),
        "tika_log_config_file": "/etc/tika/log4j_tika.xml",
        "tika_version": version,
        "heap_max": "2048",
    }

    class Config:
        env_prefix = "tika_"
