from pathlib import Path

GH_ISSUES_DEFAULT_REPOSITORY = "ol-platform-eng/concourse-workflow"
PACKER_WATCHED_PATHS = [
    "src/bilder/images/packer.pkr.hcl",
    "src/bilder/images/config.pkr.hcl",
    "src/bilder/images/variables.pkr.hcl",
    "src/bilder/components/",
]
PULUMI_CODE_PATH = Path("src/ol_infrastructure")
PULUMI_WATCHED_PATHS = [
    "src/ol_infrastructure/lib/",
    "src/ol_infrastructure/components/",
    "pipelines/infrastructure/scripts/",
    "src/bridge/secrets/",
]
