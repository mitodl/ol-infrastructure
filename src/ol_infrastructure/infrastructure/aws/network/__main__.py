"""
Manage the creation of VPC infrastructure and the peering relationships between them.
"""
from ol_infrastructure.components.aws.olvpc import (
    OLVPC,
    OLVPCConfig,
    OLVPCPeeringConnection
)
from pulumi import export, get_stack
from pulumi.config import get_config

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

export('data_vpc_id', data_vpc.olvpc.id)
export('data_vpc_cidr', data_vpc.olvpc.cidr_block)
export('data_vpc_cidr_v6', data_vpc.olvpc.ipv6_cidr_block)
export('data_vpc_rds_subnet', data_vpc.db_subnet_group.name)

operations_vpc_config = OLVPCConfig(
    vpc_name=get_config('operations_vpc:name'),
    cidr_block=get_config('operations_vpc:cidr_block'),
    num_subnets=4,
    tags={
        'OU': 'operations',
        'Environment': f'operations-{env_suffix}',
        'business_unit': 'operations',
        'Name': 'Operations {stack_name}'
    }
)
operations_vpc = OLVPC(operations_vpc_config)

export('operations_vpc_id', operations_vpc.olvpc.id)
export('operations_vpc_cidr', operations_vpc.olvpc.cidr_block)
export('operations_vpc_cidr_v6', operations_vpc.olvpc.ipv6_cidr_block)
export('operations_vpc_rds_subnet', operations_vpc.db_subnet_group.name)

operations_to_data_peer = OLVPCPeeringConnection(
    f'ol-operations-{env_suffix}-to-ol-data-{env_suffix}-vpc-peer',
    operations_vpc,
    data_vpc
)
