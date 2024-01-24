from pathlib import Path

PULUMI_CODE_PATH = Path("src/ol_infrastructure")
PULUMI_WATCHED_PATHS = [
    "src/ol_infrastructure/lib/",
    "src/ol_infrastructure/components/",
    "pipelines/infrastructure/scripts/",
    "src/bridge/secrets/",
]
PACKER_WATCHED_PATHS = [
    "src/bilder/images/packer.pkr.hcl",
    "src/bilder/images/config.pkr.hcl",
    "src/bilder/images/variables.pkr.hcl",
    "src/bilder/components/",
]
