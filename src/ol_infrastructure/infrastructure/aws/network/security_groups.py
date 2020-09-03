from functools import partial
from typing import Text

from pulumi_aws import ec2


def default_group(vpc: ec2.Vpc) -> ec2.AwaitableGetSecurityGroupResult:
    return ec2.get_security_group(
        vpc_id=vpc.id,  # type: ignore
        name='default'
    )


def public_web(vpc_name: Text, vpc: ec2.Vpc) -> partial:
    """Create a security group that exposes a webserver to the public internet.

    :param vpc_name: The name of the VPC where the security group is being created.
    :type vpc_name: Text

    :param vpc: The VPC instance that the security group is being created in.
    :type vpc: ec2.Vpc

    :returns: A partial SecurityGroup object that can be finalized in the importing module

    :rtype: partial
    """
    return partial(
        ec2.SecurityGroup,
        f'{vpc_name}-web-server',
        description='HTTP/HTTPS access from the public internet',
        vpc_id=vpc.id,
        ingress=[
            {
                'from_port': 80,
                'to_port': 80,
                'protocol': 'tcp',
                'cidr_blocks': ['0.0.0.0/0'],
                'ipv6_cidr_blocks': ['::/0'],
                'description': 'HTTP access from the public internet'
            }, {
                'from_port': 443,
                'to_port': 443,
                'protocol': 'tcp',
                'cidr_blocks': ['0.0.0.0/0'],
                'ipv6_cidr_blocks': ['::/0'],
                'description': 'HTTPS access from the public internet'
            }
        ]
    )


def salt_minion(vpc_name: Text, vpc: ec2.Vpc, ops_vpc: ec2.Vpc) -> partial:
    """Create a security group to allow access to Salt minions from the appropriate Salt master.

    :param vpc_name: The name of the VPC that the security group is being created in.
    :type vpc_name: Text

    :param vpc: The VPC instance that the security group is being created in.
    :type vpc: ec2.Vpc

    :param ops_vpc: The VPC instance that the target Salt master is located in.
    :type ops_vpc: ec2.Vpc

    :returns: A partial SecurityGroup object that can be finalized in the importing module

    :rtype: partial
    """
    return partial(
        ec2.SecurityGroup,
        f'{vpc_name}-salt-minion',
        description='Access to minions from the salt master',
        vpc_id=vpc.id,
        ingress=[
            {
                'from_port': 22,
                'to_port': 22,
                'protocol': 'tcp',
                'cidr_blocks': [ops_vpc.cidr_block],
                'description': 'SSH access from the salt master'
            }, {
                'from_port': 19999,
                'to_port': 19999,
                'protocol': 'tcp',
                'cidr_blocks': [ops_vpc.cidr_block],
                'description': 'Access to the Netdata HTTP interface from the salt master'
            }
        ]
    )
