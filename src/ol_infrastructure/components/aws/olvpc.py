"""This module defines a Pulumi component resource for building an AWS VPC.

This includes:

- Create the named VPC with appropriate tags
- Create a minimum of 3 subnets across multiple availability zones
- Create an internet gateway
- Create an IPv6 egress gateway
- Create a route table and associate the created subnets with it
- Create a routing table to include the relevant peers and their networks
- Create an RDS subnet group

"""

from functools import partial
from ipaddress import IPv4Network, IPv6Network
from itertools import cycle
from typing import Optional

from pulumi import ComponentResource, ResourceOptions
from pulumi_aws import ec2, elasticache, rds
from pydantic import PositiveInt, ValidationInfo, field_validator

from ol_infrastructure.lib.aws.ec2_helper import (
    availability_zones,
    internet_gateway_opts,
    route_table_opts,
    subnet_opts,
    vpc_opts,
    vpc_peer_opts,
)
from ol_infrastructure.lib.ol_types import AWSBase

MIN_SUBNETS = PositiveInt(3)
MAX_NET_PREFIX = (
    21  # A CIDR block of prefix length 21 allows for up to 8 /24 subnet blocks
)
SUBNET_PREFIX_V4 = (
    24  # A CIDR block of prefix length 24 allows for up to 255 individual IP addresses
)
SUBNET_PREFIX_V6 = 64


def subnet_v6(subnet_number: int, cidr_block: IPv6Network) -> str:
    network = IPv6Network(cidr_block)
    subnets = network.subnets(new_prefix=SUBNET_PREFIX_V6)
    return str(list(subnets)[subnet_number])


class OLVPCConfig(AWSBase):
    """Schema definition for VPC configuration values."""

    vpc_name: str
    cidr_block: IPv4Network
    k8s_service_subnet: Optional[IPv4Network] = None
    k8s_pod_subnet: Optional[IPv4Network] = None
    num_subnets: PositiveInt = MIN_SUBNETS
    enable_ipv6: bool = True
    default_public_ip: bool = True

    @field_validator("cidr_block")
    @classmethod
    def is_private_net(cls, network: IPv4Network) -> IPv4Network:
        """Ensure that only private subnets are assigned to VPC.

        :param network: CIDR block configured for the VPC to be created
        :type network: IPv4Network

        :raises ValueError: Raise a ValueError if the CIDR block is not for an RFC1918
            private network, or is too small

        :returns: IPv4Network object passed to validator function

        :rtype: IPv4Network
        """
        if not network.is_private:
            msg = "Specified CIDR block for VPC is not an RFC1918 private network"
            raise ValueError(msg)
        if network.prefixlen > MAX_NET_PREFIX:
            msg = "Specified CIDR block has a prefix that is too large. Please specify a network with a prefix length between /16 and /21"  # noqa: E501
            raise ValueError(msg)
        return network

    # TODO @Ardiea: Add verification of the pod subnet and ensure
    # that it doesn't overlap with service subnet
    @field_validator("k8s_service_subnet")
    @classmethod
    def k8s_service_subnet_is_subnet(
        cls, k8s_service_subnet: Optional[IPv4Network], info: ValidationInfo
    ) -> Optional[IPv4Network]:
        """Ensure that specified k8s subnet is actually a subnet of the cidr specified
            for the VPC.

        :param k8s_service_subnet: The K8S service subnet to be created in the VPC.
        :type k8s_service_subnet: IPv4Network

        :param values: Dictonary containing the rest of the class values
        :type values: Dict

        :raises ValueError: Raise a ValueError if the specified subnet is not actually a
            subnet of the VPC cidr

        :returns: The K8S service subnet

        :rtype: IPv4Network
        """
        network = info.data["cidr_block"]
        assert network is not None  # noqa: S101
        if k8s_service_subnet is not None and not k8s_service_subnet.subnet_of(network):
            msg = f"{k8s_service_subnet} is not a subnet of {network}"
            raise ValueError(msg)
        return k8s_service_subnet

    @field_validator("num_subnets")
    @classmethod
    def min_subnets(cls, num_nets: PositiveInt) -> PositiveInt:
        """Enforce that no fewer than the minimum number of subnets are created.

        :param num_nets: Number of subnets to be created in the VPC
        :type num_nets: PositiveInt

        :raises ValueError: Raise a ValueError if the number of subnets is fewer than
            MIN_SUBNETS

        :returns: The number of subnets to be created in the VPC

        :rtype: PositiveInt
        """
        if num_nets < MIN_SUBNETS:
            msg = "There should be at least 2 subnets defined for a VPC to allow for high availability across availability zones"  # noqa: E501
            raise ValueError(msg)
        return num_nets


class OLVPC(ComponentResource):
    """Pulumi component for building all of the necessary pieces of an AWS VPC.

    A component resource that encapsulates all of the standard practices of how the Open
    Learning Platform Engineering team constructs and organizes VPC environments in AWS.
    """

    def __init__(self, vpc_config: OLVPCConfig, opts: Optional[ResourceOptions] = None):
        """Build an AWS VPC with subnets, internet gateway, and routing table.

        :param vpc_config: Configuration object for customizing the created VPC and
            associated resources.
        :type vpc_config: OLVPCConfig

        :param opts: Optional resource options to be merged into the defaults.  Useful
            for handling things like AWS provider overrides.
        :type opts: Optional[ResourceOptions]
        """
        super().__init__("ol:infrastructure:aws:VPC", vpc_config.vpc_name, None, opts)
        resource_options = ResourceOptions.merge(
            ResourceOptions(parent=self),
            opts,
        )
        self.vpc_config = vpc_config
        vpc_resource_opts, imported_vpc_id = vpc_opts(
            vpc_config.cidr_block, vpc_config.tags
        )
        self.olvpc = ec2.Vpc(
            vpc_config.vpc_name,
            cidr_block=str(vpc_config.cidr_block),
            enable_dns_support=True,
            enable_dns_hostnames=True,
            assign_generated_ipv6_cidr_block=vpc_config.enable_ipv6,
            tags=vpc_config.tags,
            opts=ResourceOptions.merge(resource_options, vpc_resource_opts),
        )

        internet_gateway_resource_opts, imported_gateway_id = internet_gateway_opts(
            imported_vpc_id
        )
        self.gateway = ec2.InternetGateway(
            f"{vpc_config.vpc_name}-internet-gateway",
            vpc_id=self.olvpc.id,
            tags=vpc_config.tags,
            opts=ResourceOptions.merge(
                resource_options, internet_gateway_resource_opts
            ),
        )

        self.egress_gateway = ec2.EgressOnlyInternetGateway(
            f"{vpc_config.vpc_name}-egress-internet-gateway",
            opts=resource_options,
            vpc_id=self.olvpc.id,
            tags=vpc_config.tags,
        )

        route_table_resource_opts, imported_route_table_id = route_table_opts(
            imported_gateway_id
        )
        self.route_table = ec2.RouteTable(
            f"{vpc_config.vpc_name}-route-table",
            tags=vpc_config.tags,
            vpc_id=self.olvpc.id,
            opts=ResourceOptions.merge(resource_options, route_table_resource_opts),
        )

        ec2.Route(
            f"{vpc_config.vpc_name}-default-external-network-route",
            route_table_id=self.route_table.id,
            destination_cidr_block="0.0.0.0/0",
            gateway_id=self.gateway.id,
        )
        ec2.Route(
            f"{vpc_config.vpc_name}-default-external-ipv6-network-route",
            route_table_id=self.route_table.id,
            destination_ipv6_cidr_block="::/0",
            egress_only_gateway_id=self.egress_gateway.id,
        )

        self.olvpc_subnets: list[ec2.Subnet] = []
        zones: list[str] = availability_zones(vpc_config.region)
        subnet_iterator = zip(
            range(vpc_config.num_subnets),
            cycle(zones),
            vpc_config.cidr_block.subnets(new_prefix=SUBNET_PREFIX_V4),
        )

        for index, zone, subnet_v4 in subnet_iterator:
            net_name = f"{vpc_config.vpc_name}-subnet-{index + 1}"
            subnet_resource_opts, imported_subnet_id = subnet_opts(
                subnet_v4, imported_vpc_id
            )
            if imported_subnet_id:
                subnet = ec2.get_subnet(id=imported_subnet_id)
                zone = subnet.availability_zone  # noqa: PLW2901
            ol_subnet = ec2.Subnet(
                net_name,
                cidr_block=str(subnet_v4),
                ipv6_cidr_block=self.olvpc.ipv6_cidr_block.apply(
                    partial(subnet_v6, index)
                ),
                availability_zone=zone,
                vpc_id=self.olvpc.id,
                map_public_ip_on_launch=vpc_config.default_public_ip,
                tags=vpc_config.merged_tags({"Name": net_name}),
                assign_ipv6_address_on_creation=True,
                opts=ResourceOptions.merge(resource_options, subnet_resource_opts),
            )
            ec2.RouteTableAssociation(
                f"{net_name}-route-table-association",
                subnet_id=ol_subnet.id,
                route_table_id=self.route_table.id,
                opts=resource_options,
            )
            self.olvpc_subnets.append(ol_subnet)
        self.k8s_service_subnet = None
        self.k8s_pod_subnet = None
        if vpc_config.k8s_service_subnet and vpc_config.k8s_pod_subnet:
            service_subnet_third_octet = (
                int(vpc_config.k8s_service_subnet.network_address) & 0xFF00
            ) >> 8
            self.k8s_service_subnet = ec2.Subnet(
                f"{vpc_config.vpc_name}-k8s-service-subnet",
                cidr_block=str(vpc_config.k8s_service_subnet),
                ipv6_cidr_block=self.olvpc.ipv6_cidr_block.apply(
                    partial(subnet_v6, service_subnet_third_octet)
                ),
                vpc_id=self.olvpc.id,
                tags=vpc_config.merged_tags(
                    {"Name": f"{vpc_config.vpc_name}-k8s-service-subnet"}
                ),
                assign_ipv6_address_on_creation=True,
                opts=resource_options,
            )
            ec2.RouteTableAssociation(
                f"{vpc_config.vpc_name}-k8s-service-subnet-route-table-association",
                subnet_id=self.k8s_service_subnet.id,
                route_table_id=self.route_table.id,
                opts=resource_options,
            )

            pod_subnet_third_octet = (
                int(vpc_config.k8s_pod_subnet.network_address) & 0xFF00
            ) >> 8
            self.k8s_pod_subnet = ec2.Subnet(
                f"{vpc_config.vpc_name}-k8s-pod-subnet",
                cidr_block=str(vpc_config.k8s_pod_subnet),
                ipv6_cidr_block=self.olvpc.ipv6_cidr_block.apply(
                    partial(subnet_v6, pod_subnet_third_octet)
                ),
                vpc_id=self.olvpc.id,
                tags=vpc_config.merged_tags(
                    {"Name": f"{vpc_config.vpc_name}-k8s-pod-subnet"}
                ),
                assign_ipv6_address_on_creation=True,
                opts=resource_options,
            )
            ec2.RouteTableAssociation(
                f"{vpc_config.vpc_name}-k8s-pod-subnet-route-table-association",
                subnet_id=self.k8s_pod_subnet.id,
                route_table_id=self.route_table.id,
                opts=resource_options,
            )

        self.db_subnet_group = rds.SubnetGroup(
            f"{vpc_config.vpc_name}-db-subnet-group",
            opts=resource_options,
            description=f"RDS subnet group for {vpc_config.vpc_name}",
            name=f"{vpc_config.vpc_name}-db-subnet-group",
            subnet_ids=[net.id for net in self.olvpc_subnets],
            tags=vpc_config.tags,
        )

        self.cache_subnet_group = elasticache.SubnetGroup(
            f"{vpc_config.vpc_name}-cache-subnet-group",
            opts=resource_options,
            description=f"Elasticache subnet group for {vpc_config.vpc_name}",
            name=f"{vpc_config.vpc_name}-cache-subnet-group",
            subnet_ids=[net.id for net in self.olvpc_subnets],
        )

        self.s3_endpoint = ec2.VpcEndpoint(
            f"{vpc_config.vpc_name}-s3",
            service_name="com.amazonaws.us-east-1.s3",
            vpc_id=self.olvpc.id,
            route_table_ids=[self.route_table.id],
            tags=vpc_config.tags,
            opts=ResourceOptions(parent=self),
        )
        outputs = {
            "olvpc": self.olvpc,
            "subnets": self.olvpc_subnets,
            "route_table": self.route_table,
            "rds_subnet_group": self.db_subnet_group,
        }
        if self.k8s_service_subnet:
            outputs["k8s_service_subnet"] = self.k8s_service_subnet
        self.register_outputs(outputs)


class OLVPCPeeringConnection(ComponentResource):
    """Component for creating a VPC peering connection and populating routes."""

    def __init__(
        self,
        vpc_peer_name: str,
        source_vpc: OLVPC,
        destination_vpc: OLVPC,
        opts: Optional[ResourceOptions] = None,
    ):
        """Create a peering connection and associated routes between two managed VPCs.

        :param vpc_peer_name: The name of the peering connection
        :type vpc_peer_name: str

        :param source_vpc: The source VPC object to be used as one end of the peering
            connection.
        :type source_vpc: OLVPC

        :param destination_vpc: The destination VPC object to be used as the other end
            of the peering connection
        :type destination_vpc: OLVPC

        :param opts: Resource option definitions to propagate to the child resources
        :type opts: Optional[ResourceOptions]
        """
        super().__init__(
            "ol:infrastructure:aws:VPCPeeringConnection", vpc_peer_name, None, opts
        )
        resource_options = ResourceOptions.merge(
            ResourceOptions(parent=self),
            opts,
        )
        vpc_peer_resource_opts, imported_vpc_peer_id = vpc_peer_opts(
            str(source_vpc.vpc_config.cidr_block),
            str(destination_vpc.vpc_config.cidr_block),
        )
        self.peering_connection = ec2.VpcPeeringConnection(
            f"{source_vpc.vpc_config.vpc_name}-to-{destination_vpc.vpc_config.vpc_name}-vpc-peer",
            auto_accept=True,
            vpc_id=source_vpc.olvpc.id,
            peer_vpc_id=destination_vpc.olvpc.id,
            tags=source_vpc.vpc_config.merged_tags(
                {
                    "Name": f"{source_vpc.vpc_config.vpc_name} to {destination_vpc.vpc_config.vpc_name} peer"  # noqa: E501
                }
            ),
            opts=resource_options.merge(vpc_peer_resource_opts),
        )
        self.source_to_dest_route = ec2.Route(
            f"{source_vpc.vpc_config.vpc_name}-to-{destination_vpc.vpc_config.vpc_name}-route",
            route_table_id=source_vpc.route_table.id,
            destination_cidr_block=destination_vpc.olvpc.cidr_block,
            vpc_peering_connection_id=self.peering_connection.id,
            opts=resource_options,
        )
        self.dest_to_source_route = ec2.Route(
            f"{destination_vpc.vpc_config.vpc_name}-to-{source_vpc.vpc_config.vpc_name}-route",
            route_table_id=destination_vpc.route_table.id,
            destination_cidr_block=source_vpc.olvpc.cidr_block,
            vpc_peering_connection_id=self.peering_connection.id,
            opts=resource_options,
        )
        self.register_outputs({})
