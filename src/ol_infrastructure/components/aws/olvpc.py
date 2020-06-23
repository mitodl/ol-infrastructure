# coding: utf-8
"""This module defines a Pulumi component resource for encapsulating our best practices for building an AWS VPC.

This includes:

- Create the named VPC with appropriate tags
- Create a minimum of 3 subnets across multiple availability zones
- Create an internet gateway
- Create an IPv6 egress gateway
- Create a route table and associate the created subnets with it
- Create a routing table to include the relevant peers and their networks
- Create an RDS subnet group
"""
from ipaddress import IPv4Network, IPv6Network
from itertools import cycle
from typing import Dict, List, Optional, Text

from pulumi import (
    ComponentResource,
    Output,
    ResourceOptions,
    ResourceTransformationArgs,
    ResourceTransformationResult
)
from pulumi_aws import ec2, rds
from pydantic import PositiveInt, validator

from ol_infrastructure.lib.aws.ec2_helper import availability_zones
from ol_infrastructure.lib.ol_types import AWSBase, BusinessUnit

MIN_SUBNETS = PositiveInt(3)
MAX_NET_PREFIX = 21  # A CIDR block of prefix length 21 allows for up to 8 /24 subnet blocks
SUBNET_PREFIX_V4 = 24  # A CIDR block of prefix length 24 allows for up to 255 individual IP addresses
SUBNET_PREFIX_V6 = 64

class OLVPCConfig(AWSBase):
    """Schema definition for VPC configuration values."""
    vpc_name: Text
    cidr_block: IPv4Network
    num_subnets: PositiveInt = MIN_SUBNETS
    enable_ipv6: bool = True
    default_public_ip: bool = True

    @validator('cidr_block')
    def is_private_net(cls: 'OLVPCConfig', network: IPv4Network) -> IPv4Network:  # noqa: N805
        """Ensure that only private subnets are assigned to VPC.

        :param cls: Class object of OLVPCConfig
        :type cls: OLVPCConfig

        :param network: CIDR block configured for the VPC to be created
        :type network: IPv4Network

        :raises ValueError: Raise a ValueError if the CIDR block is not for an RFC1918 private network, or is too small

        :returns: IPv4Network object passed to validator function

        :rtype: IPv4Network
        """
        if not network.is_private:
            raise ValueError('Specified CIDR block for VPC is not an RFC1918 private network')
        if network.prefixlen > MAX_NET_PREFIX:
            raise ValueError(
                'Specified CIDR block has a prefix that is too large. '
                'Please specify a network with a prefix length between /16 and /21')
        return network

    @validator('num_subnets')
    def min_subnets(cls: 'OLVPCConfig', num_nets: PositiveInt) -> PositiveInt:  # noqa: N805
        """Enforce that no fewer than the minimum number of subnets are created.

        :param cls: Class object of OLVPCConfig
        :type cls: OLVPCConfig

        :param num_nets: Number of subnets to be created in the VPC
        :type num_nets: PositiveInt

        :raises ValueError: Raise a ValueError if the number of subnets is fewer than MIN_SUBNETS

        :returns: The number of subnets to be created in the VPC

        :rtype: PositiveInt
        """
        if num_nets < MIN_SUBNETS:
            raise ValueError(
                'There should be at least 2 subnets defined for a VPC to allow for high availability '
                'across availability zones')
        return num_nets


class OLVPC(ComponentResource):
    """Pulumi component for building all of the necessary pieces of an AWS VPC.

    A component resource that encapsulates all of the standard practices of how the Open Learning Platform Engineering
    team constructs and organizes VPC environments in AWS.
    """
    def __init__(self, vpc_config: OLVPCConfig, opts: Optional[ResourceOptions] = None):  # noqa: WPS210
        """Build an AWS VPC with subnets, internet gateway, and routing table.

        :param vpc_config: Configuration object for customizing the created VPC and associated resources.
        :type vpc_config: OLVPCConfig
        """
        super().__init__('ol:infrastructure:aws:VPC', vpc_config.vpc_name, None, opts)
        resource_options = ResourceOptions(parent=self)
        # try:
        #     existing_vpcs = ec2.get_vpcs(tags={'Name': vpc_config.tags.get('Name')})
        # except:
        #     existing_vpcs = []
        # if existing_vpcs:
        #     unique_vpcs = []
        #     for vpc in existing_vpcs:
        #         if vpc not in unique_vpcs:
        #             unique_vpcs.append(vpc)
        #     if len(unique_vpcs) == 1:
        #         vpc_options = ResourceOptions.merge(
        #             resource_options, ResourceOptions(import_=unique_vpcs[0].id))
        # else:
        vpc_options = resource_options

        self.olvpc = ec2.Vpc(
            vpc_config.vpc_name,
            cidr_block=str(vpc_config.cidr_block),
            enable_dns_support=True,
            enable_dns_hostnames=True,
            assign_generated_ipv6_cidr_block=vpc_config.enable_ipv6,
            tags=vpc_config.tags,
            opts=vpc_options)

        self.gateway = ec2.InternetGateway(
            f'{vpc_config.vpc_name}-internet-gateway',
            vpc_id=self.olvpc.id,
            tags=vpc_config.tags,
            opts=resource_options)

        self.egress_gateway = ec2.EgressOnlyInternetGateway(
            f'{vpc_config.vpc_name}-egress-internet-gateway',
            opts=resource_options,
            vpc_id=self.olvpc.id,
            tags=vpc_config.tags
        )

        self.route_table = ec2.RouteTable(
            f'{vpc_config.vpc_name}-route-table',
            tags=vpc_config.tags,
            vpc_id=self.olvpc.id,
            routes=[
                {
                    'cidr_block': '0.0.0.0/0',
                    'gateway_id': self.gateway.id
                },
                {
                    "ipv6CidrBlock": "::/0",
                    "egressOnlyGatewayId": self.egress_gateway.id
                }
            ],
            opts=ResourceOptions(parent=self)
        )

        self.olvpc_subnets: List[ec2.Subnet] = []
        zones: List[Text] = availability_zones(vpc_config.region)
        v6net = self.olvpc.ipv6_cidr_block.apply(
            lambda cidr: [str(net) for net in IPv6Network(cidr).subnets(new_prefix=SUBNET_PREFIX_V6)])
        subnet_iterator = zip(
            range(vpc_config.num_subnets),
            cycle(zones),
            vpc_config.cidr_block.subnets(new_prefix=SUBNET_PREFIX_V4),
            v6net)
        for index, zone, subnet_v4, subnet_v6 in subnet_iterator:
            net_name = f'{vpc_config.vpc_name}-subnet-{index + 1}'
            ol_subnet = ec2.Subnet(
                net_name,
                cidr_block=str(subnet_v4),
                ipv6_cidr_block=subnet_v6,
                availability_zone=zone,
                vpc_id=self.olvpc.id,
                map_public_ip_on_launch=vpc_config.default_public_ip,
                tags=vpc_config.tags,
                assign_ipv6_address_on_creation=vpc_config.enable_ipv6,
                opts=ResourceOptions(parent=self))
            ec2.RouteTableAssociation(
                f'{net_name}-route-table-association',
                subnet_id=ol_subnet.id,
                route_table_id=self.route_table.id,
                opts=resource_options)
            self.olvpc_subnets.append(ol_subnet)

        self.db_subnet_group = rds.SubnetGroup(
            f'{vpc_config.vpc_name}-db-subnet-group',
            opts=resource_options,
            description=f'RDS subnet group for {vpc_config.vpc_name}',
            name=f'{vpc_config.vpc_name}-db-subnet-group',
            subnet_ids=[net.id for net in self.olvpc_subnets],
            tags=vpc_config.tags)

        ec2.VpcEndpoint(
            f'{vpc_config.vpc_name}-s3',
            service_name='com.amazonaws.us-east-1.s3',
            vpc_id=self.olvpc.id,
            tags=vpc_config.tags,
            opts=ResourceOptions(parent=self))

        self.register_outputs({
            'olvpc': self.olvpc,
            'subnets': self.olvpc_subnets,
            'route_table': self.route_table,
            'rds_subnet_group': self.db_subnet_group
        })
