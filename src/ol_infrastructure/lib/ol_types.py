from enum import Enum, unique
from typing import Dict

from pydantic import BaseModel, validator

from ol_infrastructure.lib.aws.ec2_helper import aws_regions

REQUIRED_TAGS = {"OU", "Environment"}  # noqa: WPS407
RECOMMENDED_TAGS = {"Application", "Owner"}  # noqa: WPS407


@unique
class BusinessUnit(str, Enum):  # noqa: WPS600
    """Canonical source of truth for defining valid OU tags.

    We rely on tagging AWS resources with a valid OU to allow for cost allocation to
    different business units.
    """

    bootcamps = "bootcamps"
    data = "data"  # noqa: WPS110
    digital_credentials = "digital-credentials"
    micromasters = "micromasters"
    mit_open = "mit-open"
    mitx = "mitx"
    mitx_online = "mitxonline"
    ocw = "open-courseware"
    operations = "operations"
    ovs = "odl-video"
    residential = "residential"
    starteam = "starteam"
    xpro = "mitxpro"


@unique
class Apps(str, Enum):  # noqa: WPS600
    """Canonical source of truth for defining apps."""

    bootcamps = "bootcamps"
    micromasters = "micromasters"
    ocw_build = "ocw-build"
    redash = "redash"
    dagster = "dagster"
    mitxpro_edx = "xpro-edx"
    mitx_edx = "mitx-edx"
    odl_video_service = "ovs"
    mit_open = "open"
    starcellbio = "starcellbio"


class AWSBase(BaseModel):
    """Base class for configuration objects to pass to AWS component resources."""

    tags: Dict[str, str]
    region: str = "us-east-1"

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.tags.update({"pulumi_managed": "true"})

    @validator("tags")
    def enforce_tags(cls, tags: Dict[str, str]) -> Dict[str, str]:  # noqa: N805
        if not REQUIRED_TAGS.issubset(tags.keys()):
            raise ValueError(
                "Not all required tags have been specified. Missing tags: {}".format(
                    REQUIRED_TAGS.difference(tags.keys())
                )
            )
        try:
            BusinessUnit(tags["OU"])
        except ValueError:
            raise ValueError("The OU tag specified is not a valid business unit")
        return tags

    @validator("region")
    def check_region(cls, region: str) -> str:  # noqa: N805
        if region not in aws_regions():
            raise ValueError("The specified region does not exist")
        return region

    def merged_tags(self, new_tags: Dict[str, str]) -> Dict[str, str]:
        """Return a dictionary of existing tags with the ones passed in.

        This generates a new dictionary of tags in order to allow for a broadly
        applicable set of tagsto then be updated with specific tags to be set on child
        resources in a ComponentResource class.

        :param new_tags: Dictionary of specific tags to be set on a child resource.
        :type new_tags: Dict[Text, Text]

        :returns: Merged dictionary of base tags and specific tags to be set on a child
                  resource.

        :rtype: Dict[Text, Text]
        """
        tag_dict = self.tags.copy()
        tag_dict.update(new_tags)
        return tag_dict
