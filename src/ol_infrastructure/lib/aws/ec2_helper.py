"""Helper functions for working with EC2 resources."""
from enum import Enum, unique
from functools import lru_cache
from ipaddress import IPv4Network
from types import FunctionType
from typing import Any, Dict, List, Optional, Tuple, Union

import boto3
import pulumi
import yaml
from pulumi_aws import ec2

from ol_infrastructure.providers.salt.minion import OLSaltStackMinion

ec2_client = boto3.client("ec2")
AWSFilterType = List[Dict[str, Union[str, List[str]]]]  # noqa: WPS221

debian_10_ami = ec2.get_ami(  # noqa: WPS114
    filters=[
        {"name": "image-id", "values": ["ami-0e0161137b4b30900"]},
        {"name": "virtualization-type", "values": ["hvm"]},
        {"name": "root-device-type", "values": ["ebs"]},
        {"name": "name", "values": ["debian-10-amd64*"]},
    ],
    most_recent=True,
    owners=["136693071363"],
)

default_egress_args = [
    ec2.SecurityGroupEgressArgs(
        protocol="-1",
        from_port=0,
        to_port=0,
        cidr_blocks=["0.0.0.0/0"],
        ipv6_cidr_blocks=["::/0"],
    )
]


@unique
class InstanceTypes(str, Enum):  # noqa: WPS600
    small = "t3a.small"
    medium = "t3a.medium"
    large = "t3a.large"
    general_purpose_large = "m5a.large"
    general_purpose_xlarge = "m5a.xlarge"
    high_mem_regular = "r5a.large"
    high_mem_xlarge = "r5a.xlarge"


@lru_cache
def aws_regions() -> List[str]:
    """Generate the list of regions available in AWS.

    :returns: List of AWS regions

    :rtype: List[str]
    """
    return [region["RegionName"] for region in ec2_client.describe_regions()["Regions"]]


@lru_cache
def availability_zones(region: str = "us-east-1") -> List[str]:
    """Generate a list of availability zones for a given AWS region.

    :param region: The region to be queried
    :type region: str

    :returns: The list of availability zone names for the region

    :rtype: List[str]
    """
    zones = ec2_client.describe_availability_zones(
        Filters=[{"Name": "region-name", "Values": [region]}]
    )["AvailabilityZones"]
    return [zone["ZoneName"] for zone in zones]


def _conditional_import(  # noqa: WPS210
    discover_func: FunctionType,
    filters: AWSFilterType,
    attributes_key: str,
    attribute_id_key: str,
    change_attributes_ignored: Optional[List[str]] = None,
) -> Tuple[pulumi.ResourceOptions, str]:
    """Shared logic for determining whether to import an existing AWS resource into Pulumi.

    :param discover_func: A function object to be used for looking up AWS resources.  e.g. ec2_client.describe_vpcs
    :type discover_func: FunctionType

    :param filters: A set of filters to be applied to the discovery function to narrow down the candidate resources to
        be imported.
    :type filters: AWSFilterType

    :param attributes_key: The dictionary attribute used to access the resource information returned by the discovery
        function.  e.g. Vpcs
    :type attributes_key: str

    :param attribute_id_key: The dictionary attribute where the resource ID is located in the data structure returned by
        the discovery function.  e.g. VpcId
    :type attribute_id_key: str

    :param change_attributes_ignored: A list of attributes that should be ignored when comparing the imported state with
        the specified state in the Pulumi code.  e.g. ['tags']
    :type change_attributes_ignored: str

    :raises ValueError: If more than one resource of the given type is returned then a ValueError is raised as this is
        intended to only operate on a single resource.

    :returns: A Pulumi ResourceOptions object that allows for importing unmanaged resources and the ID of the imported
              resource or an empty string if the resource doesn't exist.

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
                f"More than one resource returned with filter {filters}. Found {resources}"
            )
            raise ValueError(
                "Too many resources returned. A more precise filter is needed."
            )
        resource = resources[0]
        tags = resource["Tags"]
        resource_id = resource[attribute_id_key]
    if not tags or "pulumi_managed" in {tag["Key"] for tag in tags}:  # noqa: C412
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
    vpc_cidr: IPv4Network, vpc_tags: Dict[str, str]
) -> Tuple[pulumi.ResourceOptions, str]:
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


def internet_gateway_opts(attached_vpc_id: str) -> Tuple[pulumi.ResourceOptions, str]:
    """Look up existing internet gateways to conditionally import when building a VPC.

    :param attached_vpc_id: The ID of the VPC where the target gateway will be attached
    :type attached_vpc_id: str

    :returns: A Pulumi ResourceOptions object that includes the appropriate import parameters and the ID of the imported
              gateway or an empty string if no gateway exists.

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
) -> Tuple[pulumi.ResourceOptions, str]:
    """Look up existing EC2 subnets to conditionally import.

    :param cidr_block: The CIDR block assigned to the subnet being defined.
    :type cidr_block: IPv4Network

    :param vpc_id: The ID of the VPC to look for subnets in.
    :type vpc_id: str

    :returns: A Pulumi ResourceOptions object and the ID of the subnet to be conditionally imported.

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


def route_table_opts(internet_gateway_id: str) -> Tuple[pulumi.ResourceOptions, str]:
    """Look up an existing route table and conditionally import it.

    :param internet_gateway_id: The ID of the internet gateway associated with the target route table.
    :type internet_gateway_id: str

    :returns: A Pulumi ResourceOptions object that contains the necessary import settings.

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
) -> Tuple[pulumi.ResourceOptions, str]:
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


def build_userdata(  # noqa: WPS211
    instance_name: str,
    minion_keys: OLSaltStackMinion,
    minion_roles: List[str],
    minion_environment: str,
    salt_host: str,
    additional_cloud_config: Optional[Dict[str, Any]] = None,
    additional_salt_config: Optional[Dict[str, str]] = None,
    additional_salt_grains: Optional[Dict[str, str]] = None,
) -> pulumi.Output[str]:
    """Construct a user data dictionary for use with EC2 instances.

    :param instance_name: The value for the `Name` tag
    :type instance_name: str

    :param minion_keys: The minion keys generated from the SaltStack minion dynamic
        provider
    :type minion_keys: OLSaltStackMinion

    :param minion_roles: The list of values to assign to the `roles` grain on the minion
    :type minion_roles: List[str]

    :param minion_environment: The value to set for the `environment` grain on the
        minion
    :type minion_environment: str

    :param salt_host: The resolvable address of the host for the Salt master that the
        instance will be communicating with.
    :type salt_host: str

    :param additional_cloud_config: Additional settings to pass through to cloud-init.
        It will be merged in with the YAML document that sets the Saltstack
        configuration.
    :type additional_cloud_config: Optional[Dict[str, Any]]

    :param additional_salt_config: Additional settings to set in the salt_minion module
        of cloud-init
    :type additional_salt_config: Optional[Dict[str, str]]

    :param additional_salt_grains: Additional settings to set in the salt grains
    :type additional_salt_grains: Optional[Dict[str, str]]

    :returns: A YAML rendering of the cloud-init userdata wrapped in a Pulumi output to
              create a dependency link.

    :rtype: pulumi.Output[str]
    """

    def _build_cloud_config_string(keys) -> str:  # noqa: WPS430
        cloud_config = additional_cloud_config or {}
        # TODO (TMM 2020-09-10): Once the upstream PR is merged move to using the
        # upstream bootstrap script. https://github.com/saltstack/salt-bootstrap/pull/1498
        salt_config = {
            "bootcmd": [
                "wget -O /tmp/salt_bootstrap.sh https://raw.githubusercontent.com/mitodl/salt-bootstrap/develop/bootstrap-salt.sh",  # noqa: E501
                "chmod +x /tmp/salt_bootstrap.sh",
                "sh /tmp/salt_bootstrap.sh -N -z",
            ],
            "package_update": True,
            "salt_minion": {
                "pkg_name": "salt-minion",
                "service_name": "salt-minion",
                "config_dir": "/etc/salt",
                "conf": {
                    "id": instance_name,
                    "master": salt_host,
                    "startup_states": "highstate",
                },
                "grains": {
                    "roles": minion_roles,
                    "context": "pulumi",
                    "environment": minion_environment,
                },
                "public_key": keys[0],
                "private_key": keys[1],
            },
        }
        salt_config["salt_minion"]["conf"].update(  # type: ignore
            additional_salt_config or {}
        )
        salt_config["salt_minion"]["grains"].update(  # type: ignore
            additional_salt_grains or {}
        )
        cloud_config.update(salt_config)
        return "#cloud-config\n{yaml_data}".format(
            yaml_data=yaml.dump(cloud_config, sort_keys=True)
        )

    return pulumi.Output.all(
        minion_keys.minion_public_key, minion_keys.minion_private_key
    ).apply(_build_cloud_config_string)
