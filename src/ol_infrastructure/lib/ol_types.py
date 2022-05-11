from enum import Enum, unique
from typing import Dict

from pydantic import BaseModel, validator

from ol_infrastructure.lib.aws.ec2_helper import aws_regions

REQUIRED_TAGS = {"OU", "Environment"}
RECOMMENDED_TAGS = {"Application", "Owner"}


@unique
class BusinessUnit(str, Enum):
    """Canonical source of truth for defining valid OU tags.

    We rely on tagging AWS resources with a valid OU to allow for cost allocation to
    different business units.
    """

    bootcamps = "bootcamps"
    data = "data"
    digital_credentials = "digital-credentials"
    micromasters = "micromasters"
    mit_open = "mit-open"
    mitx = "mitx"
    mitx_online = "mitxonline"
    ocw = "open-courseware"
    operations = "operations"
    ovs = "odl-video"
    residential = "residential"
    residential_staging = "residential-staging"
    starteam = "starteam"
    xpro = "mitxpro"


@unique
class Environment(str, Enum):
    """Canonical reference for valid environment names."""

    xpro = "xpro"
    mitx_staging = "mitx-staging"
    mitx = "mitx"
    mitx_online = "mitxonline"
    applications = "applications"
    data = "data"
    operations = "operations"


@unique
class Apps(str, Enum):
    """Canonical source of truth for defining apps."""

    bootcamps = "bootcamps"
    dagster = "dagster"
    edxapp = "edxapp"
    micromasters = "micromasters"
    mit_open = "open"
    mitx_edx = "mitx-edx"
    mitxonline = "mitxonline"
    mitxonline_edx = "mitxonline-edx"
    mitxpro_edx = "xpro-edx"
    ocw_build = "ocw-build"
    odl_video_service = "ovs"
    redash = "redash"
    starcellbio = "starcellbio"
    xpro = "xpro"


class AWSBase(BaseModel):
    """Base class for configuration objects to pass to AWS component resources."""

    tags: Dict[str, str]
    region: str = "us-east-1"

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.tags.update({"pulumi_managed": "true"})

    @validator("tags")
    def enforce_tags(cls, tags: Dict[str, str]) -> Dict[str, str]:
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
    def check_region(cls, region: str) -> str:
        if region not in aws_regions():
            raise ValueError("The specified region does not exist")
        return region

    def merged_tags(self, *new_tags: Dict[str, str]) -> Dict[str, str]:
        """Return a dictionary of existing tags with the ones passed in.

        This generates a new dictionary of tags in order to allow for a broadly
        applicable set of tags to then be updated with specific tags to be set on child
        resources in a ComponentResource class.

        :param *new_tags: One or more dictionaries of specific tags to be set on
                                                                        a child resource.
        :type new_tags: Dict[Text, Text]

        :returns: Merged dictionary of base tags and specific tags to be set on a child
                  resource.

        :rtype: Dict[Text, Text]
        """
        tag_dict = self.tags.copy()
        for tags in new_tags:
            tag_dict.update(tags)
        return tag_dict
