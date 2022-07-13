from concourse.lib.models import Identifier, ResourceType


def rclone() -> ResourceType:
    return ResourceType(
        name=Identifier("rclone"),
        type="docker-image",
        source={"repository": "mitodl/concourse-rclone-resource", "tag": "latest"},
    )
