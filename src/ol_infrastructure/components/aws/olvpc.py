"""
This module defines a Pulumi component resource for encapsulating our best practices for building an AWS VPC.

This includes:

    - Create the named VPC with appropriate tags

    - Create a minimum of 3 subnets across multiple availability zones

    - Create an internet gateway

    - Establish any specified peering connections

    - Create a routing table to include the relevant peers and their networks
"""
from ipaddress import IPv4Network
from typing import List, Text

import pulumi
from pulumi_aws import ec2
from pydantic import BaseModel, validator, conint, PositiveInt

from ol_infrastructure.lib.ol_types import BusinessUnit


class OLVPCConfig(BaseModel):
    vpc_name: Text
    cidr_block: IPv4Network
    num_subnets: PositiveInt = 3
    business_unit: BusinessUnit = 'operations'
    vpc_peers: List[Text] = []
    enable_ipv6: bool = True

    @validator('cidr_block')
    def is_private_net(cls: 'OLVPCConfig', network: IPv4Network) -> IPv4Network:
        """Ensure that only private subnets are assigned to VPC.

        :param cls: Class object of OLVPCConfig
        :type cls: OLVPCConfig

        :param network: CIDR block configured for the VPC to be created
        :type network: IPv4Network

        :returns: IPv4Network object passed to validator function

        :rtype: IPv4Network
        """
        if not network.is_private:
            raise ValueError('Specified CIDR block for VPC is not an RFC1918 private network')
        return network

    @validator('num_subnets')
    def min_subnets(cls, num_nets):
        if num_nets < 2:
            raise ValueError(
                'There should be at least 2 subnets defined for a VPC to allow for high availability '
                'across availability zones')


class OLVPC(pulumi.ComponentResource):

    def __init__(self, vpc_config: OLVPCConfig):
        ec2.Vpc(
            vpc_config.vpc_name,
            cidr_block=vpc_config.cidr_block,
            enable_dns_support=True,
            enable_dns_hostnames=True,
            assign_generated_ipv6_cidr_block=vpc_config.enable_ipv6)
