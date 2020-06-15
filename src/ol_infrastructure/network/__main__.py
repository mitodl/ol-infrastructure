from ol_infrastructure.components.aws.olvpc import OLVPC, OLVPCConfig


data_vpc_config = OLVPCConfig(
    vpc_name='ol-data-production',
    cidr_block='10.2.0.0/16',
    num_subnets=3,
    tags={
        'OU': 'data',
        'Environment': 'data-production',
        'business_unit': 'data',
        'Name': 'Production Data Services'})
data_vpc = OLVPC(data_vpc_config)
