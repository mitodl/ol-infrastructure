from pulumi import get_stack, export
from pulumi.config import get_config
from pulumi_aws import ec2
from ol_infrastructure.components.aws.olvpc import OLVPC, OLVPCConfig

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
    num_subnets=3,
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

operations_to_data_peer = ec2.VpcPeeringConnection(
    f'ol-operations-{env_suffix}-to-ol-data-{env_suffix}-vpc-peer',
    auto_accept=True,
    vpc_id=operations_vpc.olvpc.id,
    peer_vpc_id=data_vpc.olvpc.id,
    tags=operations_vpc_config.merged_tags({'Name': 'Operations {stack_name } To Data {stack_name} Peer'})
)
operations_to_data_route = ec2.Route(
    f'{operations_vpc_config.vpc_name}-to-{data_vpc_config.vpc_name}-route',
    route_table_id=operations_vpc.route_table.id,
    destination_cidr_block=data_vpc.olvpc.cidr_block,
    destination_ipv6_cidr_block=data_vpc.olvpc.ipv6_cidr_block,
    vpc_peering_connection_id=operations_to_data_peer.id
)
data_to_operations_route = ec2.Route(
    f'{data_vpc_config.vpc_name}-to-{operations_vpc_config.vpc_name}-route',
    route_table_id=data_vpc.route_table.id,
    destination_cidr_block=operations_vpc.olvpc.cidr_block,
    destination_ipv6_cidr_block=operations_vpc.olvpc.ipv6_cidr_block,
    vpc_peering_connection_id=operations_to_data_peer.id
)
