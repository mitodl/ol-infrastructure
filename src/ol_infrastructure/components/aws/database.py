# coding: utf-8
"""This module defines a Pulumi component resource for encapsulating our best practices for building RDS instances.

This includes:

- Create a parameter group for the database
- Create and configure a backup policy
- Manage the root user password
- Create relevant security groups
- Create DB instance
"""
from enum import Enum
from typing import Dict, List, Optional, Text, Union

import pulumi
from pulumi_aws import rds
from pydantic import BaseModel, PositiveInt, SecretStr, conint, validator

from ol_infrastructure.lib.aws.rds_helper import (
    db_engines,
    parameter_group_family
)
from ol_infrastructure.lib.ol_types import AWSBase

# manage backup policy
# generate or retrieve password in config
# create DB security group
# storage encrypted


class StorageType(str, Enum):
    magnetic = 'standard'
    ssd = 'gp2'
    performance = 'io1'


class OLReplicaDBConfig(BaseModel):
    instance_size: Text = 'db.t3.small'
    storage_type: StorageType = StorageType.ssd
    public_access: bool = False
    security_groups: Optional[List[rds.SecurityGroup]] = None

    class Config:
        arbitrary_types_allowed = True


class OLDBConfig(AWSBase):
    engine: Text
    engine_version: Text
    instance_name: Text  # The name of the RDS instance
    password: SecretStr
    subnet_group_name: Text
    security_groups: List[rds.SecurityGroup]
    backup_days: conint(ge=0, le=35, strict=True) = 30
    db_name: Optional[Text] = None  # The name of the database schema to create
    instance_size: Text = 'db.m5.large'
    is_public: bool = False
    max_storage: Optional[PositiveInt] = None  # Set to allow for storage autoscaling
    multi_az: bool = True
    prevent_delete: bool = True
    public_access: bool = False
    read_replica: Optional[Dict] = None
    storage: PositiveInt = PositiveInt(50)
    storage_type: StorageType = StorageType.ssd
    username: Text = 'oldevops'
    read_replica: Optional[OLReplicaDBConfig] = None

    class Config:
        arbitrary_types_allowed = True

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


class OLPostgresDBConfig(OLDBConfig):
    engine: Text = 'postgres'
    engine_version: Text = '12.3'
    port: PositiveInt = PositiveInt(5432)
    parameter_overrides: List[Dict[Text, Union[Text, bool, int, float]]] = [
        {'name': 'client_encoding', 'value': 'UTF-8'},
        {'name': 'server_encoding', 'value': 'UTF-8'},
        {'name': 'log_timezone', 'value': 'UTC'},
        {'name': 'timezone', 'value': 'UTC'},
        {'name': 'rds.force_ssl', 'value': 1}
    ]


class OLMariaDBConfig(OLDBConfig):
    engine: Text = 'mariadb'
    engine_version: Text = '10.4.8'
    port: PositiveInt = PositiveInt(3306)
    parameter_overrides: List[Dict[Text, Union[Text, bool, int, float]]] = [
        {'name': 'character_set_client', 'value': 'utf8mb4'},
        {'name': 'character_set_connection', 'value': 'utf8mb4'},
        {'name': 'character_set_database', 'value': 'utf8mb4'},
        {'name': 'character_set_filesystem', 'value': 'utf8mb4'},
        {'name': 'character_set_results', 'value': 'utf8mb4'},
        {'name': 'character_set_server', 'value': 'utf8mb4'},
        {'name': 'time_zone', 'value': 'UTC'}
    ]


class OLAmazonDB(pulumi.ComponentResource):

    def __init__(self, db_config: OLDBConfig, opts: pulumi.ResourceOptions = None):
        super().__init__('ol:infrastructure:aws:rds:PostgresDB', db_config.instance_name, None, opts)

        resource_options = pulumi.ResourceOptions(parent=self)

        parameter_group = rds.ParameterGroup(
            f'{db_config.instance_name}-{db_config.engine}-parameter-group',
            family=parameter_group_family(
                db_config.engine,
                db_config.engine_version),
            opts=resource_options,
            name=f'{db_config.instance_name}-{db_config.engine}-parameter-group',
            tags=db_config.tags,
            parameters=db_config.parameter_overrides
        )

        db_instance = rds.Instance(
            f'{db_config.instance_name}-{db_config.engine}-instance',
            allocated_storage=db_config.storage,
            auto_minor_version_upgrade=True,
            backup_retention_period=db_config.backup_days,
            copy_tags_to_snapshot=True,
            db_subnet_group_name=db_config.subnet_group_name,
            deletion_protection=db_config.prevent_delete,
            engine=db_config.engine,
            engine_version=db_config.engine_version,
            final_snapshot_identifier=f'{db_config.instance_name}-{db_config.engine}-final-snapshot',
            identifier=db_config.instance_name,
            instance_class=db_config.instance_size,
            max_allocated_storage=db_config.max_storage,
            multi_az=db_config.multi_az,
            name=db_config.db_name,
            opts=resource_options,
            parameter_group_name=parameter_group.name,
            password=db_config.password,
            port=db_config.port,
            publicly_accessible=db_config.is_public,
            skip_final_snapshot=False,
            storage_encrypted=True,
            storage_type=str(db_config.storage_type),
            tags=db_config.tags,
            username=db_config.username,
            vpc_security_group_ids=[group.id for group in db_config.security_groups],
        )

        component_outputs = {
            'parameter_group': parameter_group,
            'rds_instance': db_instance
        }

        if db_config.read_replica:
            db_replica = rds.Instance(
                f'{db_config.instance_name}-{db_config.engine}-replica',
                identifier=f'{db_config.instance_name}-replica',
                instance_class=db_config.read_replica.instance_size,
                kms_key_id=db_instance.kms_key_id,
                opts=resource_options,
                publicly_accessible=db_config.read_replica.public_access,
                replicate_source_db=db_instance.id,
                storage_type=db_config.read_replica.storage_type,
                tags=db_config.tags,
                vpc_security_group_ids=(
                    db_config.read_replica.security_groups or db_config.security_groups)
            )
            component_outputs['rds_replica'] = db_replica

        self.register_outputs(component_outputs)
