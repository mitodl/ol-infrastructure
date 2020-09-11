"""Manage the creation of VPC infrastructure and the peering relationships between them.

In addition to creating the VPCs, it will also import existing ones based on the defined CIDR block.  Some of these
environments were previously created with SaltStack. In that code we defaulted to the first network being at
`x.x.1.0/24`, whereas in the OLVPC component the first subnet defaults to being at `x.x.0.0/24`. Due to this disparity,
some of the networks defined below specify 4 subnets, which results in a `x.x.0.0/24` network being created, while also
importing the remaining 3 subnets. If only 3 subnets were specified then one of the existing networks would not be
managed with Pulumi.

"""
from typing import Dict

from pulumi import export, get_stack
from pulumi.config import get_config
from security_groups import default_group, public_web, salt_minion

from ol_infrastructure.components.aws.olvpc import (
    OLVPC,
    OLVPCConfig,
    OLVPCPeeringConnection
)


def vpc_exports(vpc: OLVPC) -> Dict:
    return {
        'id': vpc.olvpc.id,
        'cidr': vpc.olvpc.cidr_block,
        'cidr_v6': vpc.olvpc.ipv6_cidr_block,
        'rds_subnet': vpc.db_subnet_group.name,
        'subnet_ids': [subnet.id for subnet in vpc.olvpc_subnets]
    }


stack = get_stack()
stack_name = stack.split('.')[-1]
env_suffix = stack_name.lower()
data_vpc_config = OLVPCConfig(
    vpc_name=f'ol-data-{env_suffix}',
    cidr_block=get_config('data_vpc:cidr_block'),
    num_subnets=3,
    tags={
        'OU': 'data',
        'Environment': f'data-{env_suffix}',
        'business_unit': 'data',
        'Name': f'{stack_name} Data Services'})
data_vpc = OLVPC(data_vpc_config)

residential_mitx_vpc_config = OLVPCConfig(
    vpc_name=f'mitx-{env_suffix}',
    cidr_block=get_config('residential_vpc:cidr_block'),
    num_subnets=4,
    tags={
        'OU': 'residential',
        'Environment': f'mitx-{env_suffix}',
        'business_unit': 'residential',
        'Name': f'MITx {stack_name}'
    }
)
residential_mitx_vpc = OLVPC(residential_mitx_vpc_config)

operations_vpc_config = OLVPCConfig(
    vpc_name=get_config('operations_vpc:name'),
    cidr_block=get_config('operations_vpc:cidr_block'),
    num_subnets=4,
    tags={
        'OU': 'operations',
        'Environment': f'operations-{env_suffix}',
        'business_unit': 'operations',
        'Name': f'Operations {stack_name}'
    }
)
operations_vpc = OLVPC(operations_vpc_config)

data_vpc_exports = vpc_exports(data_vpc)
data_vpc_exports.update(
    {
        'security_groups': {
            'default': default_group(data_vpc.olvpc).id,
            'web': public_web(
                data_vpc_config.vpc_name,
                data_vpc.olvpc
            )(
                tags=data_vpc_config.merged_tags({'Name': f'ol-data-{env_suffix}-public-web'}),
                name=f'ol-data-{env_suffix}-public-web'
            ).id,
            'salt_minion': salt_minion(
                data_vpc_config.vpc_name,
                data_vpc.olvpc,
                operations_vpc.olvpc
            )(
                tags=data_vpc_config.merged_tags({'Name': f'ol-data-{env_suffix}-salt-minion'}),
                name=f'ol-data-{env_suffix}-salt-minion'
            ).id
        }
    }
)
export('data_vpc', data_vpc_exports)

residential_mitx_vpc_exports = vpc_exports(residential_mitx_vpc)
residential_mitx_vpc_exports.update(
    {
        'security_groups': {
            'default': default_group(residential_mitx_vpc.olvpc).id,
            'web': public_web(
                residential_mitx_vpc_config.vpc_name,
                residential_mitx_vpc.olvpc
            )(
                tags=residential_mitx_vpc_config.merged_tags({'Name': f'mitx-{env_suffix}-public-web'}),
                name=f'mitx-{env_suffix}-public-web'
            ).id,
            'salt_minion': salt_minion(
                residential_mitx_vpc_config.vpc_name,
                residential_mitx_vpc.olvpc,
                operations_vpc.olvpc
            )(
                tags=residential_mitx_vpc_config.merged_tags({'Name': f'mitx-{env_suffix}-salt-minion'}),
                name=f'mitx-{env_suffix}-salt-minion'
            ).id
        }
    }
)
export('residential_mitx_vpc', residential_mitx_vpc_exports)

operations_vpc_exports = vpc_exports(operations_vpc)
export('operations_vpc', operations_vpc_exports)

operations_to_data_peer = OLVPCPeeringConnection(
    f'ol-operations-{env_suffix}-to-ol-data-{env_suffix}-vpc-peer',
    operations_vpc,
    data_vpc
)

operations_to_mitx_peer = OLVPCPeeringConnection(
    f'ol-operations-{env_suffix}-to-residential-mitx-{env_suffix}-vpc-peer',
    operations_vpc,
    residential_mitx_vpc
)

data_to_mitx_peer = OLVPCPeeringConnection(
    f'ol-data-{env_suffix}-to-residential-mitx-{env_suffix}-vpc-peer',
    data_vpc,
    residential_mitx_vpc
)
