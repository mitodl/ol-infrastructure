from typing import List, Text
from functools import lru_cache

import boto3

ec2_client = boto3.client('ec2')


@lru_cache
def aws_regions() -> List[Text]:
    """Generate the list of regions available in AWS.

    :returns: List of AWS regions

    :rtype: List[Text]
    """
    return [region['RegionName'] for region in ec2_client.describe_regions()['Regions']]


@lru_cache
def availability_zones(region: Text) -> List[Text]:
    """Generate a list of availability zones for a given AWS region.

    :param region: The region to be queried
    :type region: Text

    :returns: The list of availability zone names for the region

    :rtype: List[Text]
    """
    zones = ec2_client.describe_availability_zones(
        Filters=[{'Name': 'region-name', 'Values': [region]}]
    )['AvailabilityZones']
    return [zone['ZoneName'] for zone in zones]
