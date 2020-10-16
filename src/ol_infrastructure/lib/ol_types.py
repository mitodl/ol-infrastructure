from enum import Enum, unique
from typing import Dict, Text

from pydantic import BaseModel, validator

from ol_infrastructure.lib.aws.ec2_helper import aws_regions

REQUIRED_TAGS = {'OU', 'Environment'}  # noqa: WPS407


@unique
class BusinessUnit(str, Enum):  # noqa: WPS600
    """Canonical source of truth for defining valid OU tags to be used for cost allocation purposes."""

    bootcamps = 'bootcamps'
    data = 'data'  # noqa: WPS110
    digital_credentials = 'digital-credentials'
    micromasters = 'micromasters'
    mit_open = 'mit-open'
    mitx = 'mitx'
    ocw = 'open-courseware'
    operations = 'operations'
    ovs = 'odl-video'
    residential = 'residential'
    starteam = 'starteam'
    xpro = 'mitxpro'


class AWSBase(BaseModel):
    """Base class for deriving configuration objects to pass to AWS component resources."""

    tags: Dict
    region: Text = 'us-east-1'

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.tags.update({'pulumi_managed': 'true'})

    @validator('tags')
    def enforce_tags(cls, tags: Dict[Text, Text]) -> Dict[Text, Text]:  # noqa: N805
        if not REQUIRED_TAGS.issubset(tags.keys()):
            raise ValueError(
                f'Not all required tags have been specified. Missing tags: {REQUIRED_TAGS.difference(tags.keys())}')
        try:
            BusinessUnit(tags['OU'])
        except ValueError:
            raise ValueError('The OU tag specified is not a valid business unit')
        return tags

    @validator('region')
    def check_region(cls, region: Text) -> Text:  # noqa: N805
        if region not in aws_regions():
            raise ValueError('The specified region does not exist')
        return region

    def merged_tags(self, new_tags: Dict[Text, Text]) -> Dict[Text, Text]:
        """Return a dictionary of tags that merges those defined on the class with those passed in.

        This generates a new dictionary of tags in order to allow for a broadly applicable set of tagsto then be updated
        with specific tags to be set on child resources in a ComponentResource class.

        :param new_tags: Dictionary of specific tags to be set on a child resource.
        :type new_tags: Dict[Text, Text]

        :returns: Merged dictionary of base tags and specific tags to be set on a child resource.

        :rtype: Dict[Text, Text]
        """
        tag_dict = self.tags.copy()
        tag_dict.update(new_tags)
        return tag_dict
