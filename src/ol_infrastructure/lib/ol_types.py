from enum import Enum
from typing import Dict, Text

from pydantic import BaseModel, validator

from ol_infrastructure.lib.aws.ec2_helper import aws_regions

REQUIRED_TAGS = {'OU', 'Environment'}


class BusinessUnit(str, Enum):  # noqa: WPS600
    bootcamps = 'bootcamps'
    micromasters = 'micromasters'
    xpro = 'mitxpro'
    ovs = 'odl-video'
    ocw = 'open-courseware'
    operations = 'operations'
    residential = 'residential'
    starteam = 'starteam'
    mit_open = 'mit-open'


class AWSBase(BaseConfig):
    tags: Dict[Text, Text]
    region: Text = 'us-east-1'

    @validator('tags')
    def enforce_tags(cls: 'AWSBase', tags: Dict[Text, Text]) -> Dict[Text, Text]:
        if not REQUIRED_TAGS.issubset(tags.keys()):
            raise ValueError(
                f'Not all required tags have been specified. Missing tags: {REQUIRED_TAGS.difference(tags.keys())}')
        return tags

    @validator(region)
    def check_region(cls: 'AWSBase', region: Text) -> Text:
        if region not in aws_regions():
            raise ValueError('The specified region does not exist')
        return region
