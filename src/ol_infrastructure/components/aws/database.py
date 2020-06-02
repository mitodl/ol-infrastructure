import pulumi
from pulumi_aws import rds


# ensure db subnet group
# create parameter group
# manage backup policy
# generate or retrieve password in config
# create DB security group
# storage encrypted
# register DB in Consul

rds.SubnetGroup

class AmazonPostgresDB(pulumi.ComponentResource):

    def __init__(self, db_name: str, vpc_name: str, postgres_version: str='12.2', multi_az: bool=True):
        super().__init__('ol:infrastructure:AmazonPostgresDB', db_name)
