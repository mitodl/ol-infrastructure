# coding: utf-8
"""This module defines a Pulumi component resource for encapsulating our best practices for building RDS instances.

This includes:

- Create a parameter group for the database
- Create and configure a backup policy
- Manage the root user password
- Create relevant security groups
- Create DB instance
"""
from typing import Text, Dict

import pulumi
from pulumi_aws import rds
from pydantic import validator

from ol_infrastructure.lib.ol_types import AWSBase
from ol_infrastructure.lib.aws.rds_helper import db_engines, parameter_group_family

# ensure db subnet group
# create parameter group
# manage backup policy
# generate or retrieve password in config
# create DB security group
# storage encrypted


class OLDBConfig(AWSBase):
    db_name: Text
    engine: Text
    target_vpc_id: Text
    engine_version: Text
    multi_az: bool = True
    public_access: bool = False

    @validator('engine')
    def is_valid_engine(cls: 'OLDBConfig', engine: Text) -> Text:
        valid_engines = db_engines()
        if engine not in valid_engines:
            raise ValueError('The specified DB engine is not a valid option in AWS.')
        return engine


    @validator('engine_version')
    def is_valid_version(cls: 'OLDBConfig', engine_version: Text, values: Dict) -> Text:
        print(values)
        engine = values.get('engine')
        engines_map = db_engines()
        if engine_version not in engines_map.get(engine, []):
            raise ValueError(f'The specified version of the {engine} engine is nut supported in AWS.')
        return engine_version


class AmazonPostgresDB(pulumi.ComponentResource):

    def __init__(self, db_config: OLDBConfig, opts: pulumi.ResourceOptions = None):
        super().__init__('ol:infrastructure:AmazonPostgresDB', db_config.db_name, None, opts)
