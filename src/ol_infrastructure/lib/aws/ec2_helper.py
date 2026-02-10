"""Helper functions for working with EC2 resources."""

from enum import StrEnum, unique
from functools import lru_cache
from ipaddress import IPv4Network
from types import FunctionType

import boto3
import pulumi
from botocore.exceptions import ClientError
from pulumi_aws import ec2

ec2_client = boto3.client("ec2")
AWSFilterType = list[dict[str, str | list[str]]]

default_egress_args = [
    ec2.SecurityGroupEgressArgs(
        protocol="-1",
        from_port=0,
        to_port=0,
        cidr_blocks=["0.0.0.0/0"],
        ipv6_cidr_blocks=["::/0"],
    )
]


def is_valid_instance_type(instance_type):
    try:
        ec2_client.describe_instance_types(InstanceTypes=[instance_type])
        return True  # noqa: TRY300
    except ClientError:
        return False


@unique
class InstanceClasses(StrEnum):
    general_purpose_amd = "m7a"
    general_purpose_intel = "m7i"
    memory_optimized_amd = "r7a"
    memory_optimized_intel = "r7i"
    compute_optimized_amd = "c7a"
    compute_optimized_intel = "c7i"


@unique
class InstanceTypes(StrEnum):
    burstable_nano = "t3a.nano"
    burstable_micro = "t3a.micro"
    burstable_small = "t3a.small"
    burstable_medium = "t3a.medium"
    burstable_large = "t3a.large"
    burstable_xlarge = "t3a.xlarge"
    general_purpose_large = "m7a.large"
    general_purpose_xlarge = "m7a.xlarge"
    general_purpose_2xlarge = "m7a.2xlarge"
    general_purpose_intel_large = "m7i.large"
    general_purpose_intel_xlarge = "m7i.xlarge"
    general_purpose_intel_2xlarge = "m7i.2xlarge"
    high_mem_regular = "r7a.large"
    high_mem_xlarge = "r7a.xlarge"
    high_mem_2xlarge = "r7a.2xlarge"
    high_mem_4xlarge = "r7a.4xlarge"
    high_mem_8xlarge = "r7a.8xlarge"
    gpu_xlarge = "g4dn.xlarge"
    gpu_2xlarge = "g4dn.2xlarge"

    @classmethod
    def dereference(cls, instance_specifier) -> str:
        try:
            instance_type = cls[instance_specifier].value
        except KeyError:
            # The instance type specified is a direct specifier (e.g. t3a.large)
            if is_valid_instance_type(instance_specifier):
                instance_type = instance_specifier
            else:
                raise ValueError from None
        return instance_type


@unique
class DiskTypes(StrEnum):
    magnetic = "standard"
    legacy_ssd = "gp2"
    ssd = "gp3"
    legacy_provisioned_iops = "io1"
    provisioned_iops = "io2"


@lru_cache
def aws_regions() -> list[str]:
    """Generate the list of regions available in AWS.

    :returns: List of AWS regions

    :rtype: List[str]
    """
    return [region["RegionName"] for region in ec2_client.describe_regions()["Regions"]]


@lru_cache
def availability_zones(region: str = "us-east-1") -> list[str]:
    """Generate a list of availability zones for a given AWS region.

    :param region: The region to be queried
    :type region: str

    :returns: The list of availability zone names for the region

    :rtype: List[str]
    """
    zones = ec2_client.describe_availability_zones(
        Filters=[{"Name": "region-name", "Values": [region]}]
    )["AvailabilityZones"]
    # Avoid using us-east-1e because it doesn't support newer instance types
    return [zone["ZoneName"] for zone in zones if zone["ZoneName"] != "us-east-1e"]


def _conditional_import(
    discover_func: FunctionType,
    filters: AWSFilterType,
    attributes_key: str,
    attribute_id_key: str,
    change_attributes_ignored: list[str] | None = None,
) -> tuple[pulumi.ResourceOptions, str]:
    """Determine whether to import an existing AWS resource into Pulumi.

    :param discover_func: A function object to be used for looking up AWS resources.
        e.g. ec2_client.describe_vpcs
    :type discover_func: FunctionType

    :param filters: A set of filters to be applied to the discovery function to narrow
        down the candidate resources to be imported.
    :type filters: AWSFilterType

    :param attributes_key: The dictionary attribute used to access the resource
        information returned by the discovery function.  e.g. Vpcs
    :type attributes_key: str

    :param attribute_id_key: The dictionary attribute where the resource ID is located
        in the data structure returned by the discovery function.  e.g. VpcId
    :type attribute_id_key: str

    :param change_attributes_ignored: A list of attributes that should be ignored when
        comparing the imported state with the specified state in the Pulumi code.  e.g.
        ['tags']
    :type change_attributes_ignored: str

    :raises ValueError: If more than one resource of the given type is returned then a
        ValueError is raised as this is intended to only operate on a single resource.

    :returns: A Pulumi ResourceOptions object that allows for importing unmanaged
              resources and the ID of the imported resource or an empty string if the
              resource doesn't exist.

    :rtype: Tuple[pulumi.ResourceOptions, str]
    """
    resources = discover_func(Filters=filters)[attributes_key]
    tags = []
    resource_id = ""
    if change_attributes_ignored is None:
        change_attributes_ignored = ["tags"]
    if resources:
        if len(resources) > 1:
            pulumi.log.info(
                f"More than one resource returned with filter {filters}. "
                f"Found {resources}"
            )
            msg = "Too many resources returned. A more precise filter is needed."
            raise ValueError(msg)
        resource = resources[0]
        tags = resource["Tags"]
        resource_id = resource[attribute_id_key]
    if not tags or "pulumi_managed" in {tag["Key"] for tag in tags}:
        opts = pulumi.ResourceOptions()
    else:
        opts = pulumi.ResourceOptions(
            import_=resource_id, ignore_changes=change_attributes_ignored
        )
        if not pulumi.runtime.is_dry_run():
            ec2_client.create_tags(
                Resources=[resource_id],
                Tags=[{"Key": "pulumi_managed", "Value": "true"}],
            )
    return opts, resource_id


def vpc_opts(
    vpc_cidr: IPv4Network, vpc_tags: dict[str, str]
) -> tuple[pulumi.ResourceOptions, str]:
    """Look up and conditionally import an existing VPC.

    :param vpc_cidr: The IPv4 CIDR block of the target VPC to be imported if it exists.
        This ensures that there is no accidental overlap of IPv4 ranges.
    :type vpc_cidr: IPv4Network

    :param vpc_tags: The tags to filter the VPC lookup by to resolve cases where the
        CIDR block might overlap with an existing VPC.
    :type vpc_tags: Dict[str, str]

    :returns: A Pulumi ResourceOptions object that allows for importing unmanaged VPCs
              and the ID of the imported VPC or and empty string if the VPC doesn't
              exist.

    :rtype: Tuple[pulumi.ResourceOptions, str]
    """
    return _conditional_import(
        ec2_client.describe_vpcs,
        [
            {"Name": "cidr", "Values": [str(vpc_cidr)]},
            {"Name": "tag:Name", "Values": [vpc_tags["Name"]]},
        ],
        "Vpcs",
        "VpcId",
    )


def internet_gateway_opts(attached_vpc_id: str) -> tuple[pulumi.ResourceOptions, str]:
    """Look up existing internet gateways to conditionally import when building a VPC.

    :param attached_vpc_id: The ID of the VPC where the target gateway will be attached
    :type attached_vpc_id: str

    :returns: A Pulumi ResourceOptions object that includes the appropriate import
              parameters and the ID of the imported gateway or an empty string if no
              gateway exists.

    :rtype: Tuple[pulumi.ResourceOptions, str]
    """
    return _conditional_import(
        ec2_client.describe_internet_gateways,
        [{"Name": "attachment.vpc-id", "Values": [attached_vpc_id]}],
        "InternetGateways",
        "InternetGatewayId",
    )


def subnet_opts(
    cidr_block: IPv4Network, vpc_id: str
) -> tuple[pulumi.ResourceOptions, str]:
    """Look up existing EC2 subnets to conditionally import.

    :param cidr_block: The CIDR block assigned to the subnet being defined.
    :type cidr_block: IPv4Network

    :param vpc_id: The ID of the VPC to look for subnets in.
    :type vpc_id: str

    :returns: A Pulumi ResourceOptions object and the ID of the subnet to be
              conditionally imported.

    :rtype: Tuple[pulumi.ResourceOptions, str]
    """
    return _conditional_import(
        ec2_client.describe_subnets,
        [
            {"Name": "cidr", "Values": [str(cidr_block)]},
            {"Name": "vpc-id", "Values": [vpc_id]},
        ],
        "Subnets",
        "SubnetId",
        [
            "tags",
            "assign_ipv6_address_on_creation",
            "map_public_ip_on_launch",
            "ipv6_cidr_block",
        ],
    )


def route_table_opts(internet_gateway_id: str) -> tuple[pulumi.ResourceOptions, str]:
    """Look up an existing route table and conditionally import it.

    :param internet_gateway_id: The ID of the internet gateway associated with the
        target route table.
    :type internet_gateway_id: str

    :returns: A Pulumi ResourceOptions object that contains the necessary import
              settings.

    :rtype: Tuple[pulumi.ResourceOptions, str]
    """
    return _conditional_import(
        ec2_client.describe_route_tables,
        [{"Name": "route.gateway-id", "Values": [internet_gateway_id]}],
        "RouteTables",
        "RouteTableId",
        ["tags", "routes"],
    )


def vpc_peer_opts(
    source_vpc_cidr: str, destination_vpc_cidr: str
) -> tuple[pulumi.ResourceOptions, str]:
    """Look up an existing route table and conditionally import it.

    :param source_vpc_cidr: The CIDR block of the VPC that is initiating the peer
        request.
    :type source_vpc_cidr: str

    :param destination_vpc_cidr: The CIDR block of the VPC that is accepting the peer
        request.
    :param destination_vpc_cidr: str

    :returns: A Pulumi ResourceOptions object that contains the necessary import
              settings.

    :rtype: Tuple[pulumi.ResourceOptions, str]
    """
    return _conditional_import(
        ec2_client.describe_vpc_peering_connections,
        [
            {
                "Name": "accepter-vpc-info.cidr-block",
                "Values": [destination_vpc_cidr, source_vpc_cidr],
            },
            {
                "Name": "requester-vpc-info.cidr-block",
                "Values": [source_vpc_cidr, destination_vpc_cidr],
            },
        ],
        "VpcPeeringConnections",
        "VpcPeeringConnectionId",
        ["tags", "vpc_id", "peer_vpc_id", "id", "auto_accept"],
    )
