from functools import partial
from typing import Text

from pulumi_aws import ec2


def public_web(vpc_name: Text, vpc: ec2.Vpc) -> partial:
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
