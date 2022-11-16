from pathlib import Path

REGISTRY_IMAGE = "registry-image"
PULUMI_CODE_PATH = Path("src/ol_infrastructure")
PULUMI_WATCHED_PATHS = [  # noqa: WPS407
    "src/ol_infrastructure/lib/",
    "src/ol_infrastructure/components/",
    "pipelines/infrastructure/scripts/",
]
