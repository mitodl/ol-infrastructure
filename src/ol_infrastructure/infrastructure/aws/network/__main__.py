from pulumi import get_stack, config
from ol_infrastructure.components.aws.olvpc import OLVPC, OLVPCConfig

stack = get_stack()
stack_name = stack.split('.')[-1]
env_suffix = stack_name.lower()
data_vpc_config = OLVPCConfig(
    vpc_name=f'ol-data-{env_suffix}',
    cidr_block=config.get_config('data_vpc:cidr_block'),
    num_subnets=3,
    tags={
        'OU': 'data',
        'Environment': f'data-{env_suffix}',
        'business_unit': 'data',
        'Name': f'{stack_name} Data Services'})
data_vpc = OLVPC(data_vpc_config)
