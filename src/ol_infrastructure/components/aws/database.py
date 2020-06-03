from typing import Text

import pulumi
from pulumi_aws import rds
from pydantic import BaseModel, validator

from ol_infrastructure.lib.ol_types import BusinessUnit

# ensure db subnet group
# create parameter group
# manage backup policy
# generate or retrieve password in config
# create DB security group
# storage encrypted
# register DB in Consul


class OLDBConfig(BaseModel):
    db_name: Text
    engine: Text
    engine_version: Text
    multi_az: bool = True


rds.SubnetGroup

class AmazonPostgresDB(pulumi.ComponentResource):

    def __init__(self, db_config: OLDBConfig):
        super().__init__('ol:infrastructure:AmazonPostgresDB', db_config.db_name)
