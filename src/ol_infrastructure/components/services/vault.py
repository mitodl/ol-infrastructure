"""
This module controls mounting and configuration of database secret backends in Vault.

This includes:
- Mount a database backend at the configured mount point
- Set the configuration according to the requirements of the backend type
- Control the lease TTL settings
- Define the base set of roles according to our established best practices
"""
from enum import Enum
from pulumi import ComponentResource, ResourceOptions
from pulumi_vault import database, Mount
from pydantic import BaseModel, SecretStr
from typing import Text

DEFAULT_PORT_POSTGRES = 5432
DEFAULT_PORT_MYSQL = 3307
DEFAULT_POT_MONGODB = 27017

class DBEngines(str, Enum):
    """Constraints for valid engine types that are supported by this component."""
    postgres = 'postgresql'
    mariadb = 'mysql'
    mysql = 'mysql'
    mysql_rds = 'mysql_rds'
    mongodb = 'mongodb'

class OLVaultDatabaseConfig(BaseModel):
    db_name: Text
    mount_point: Text
    db_admin_username: Text
    db_admin_password: SecretStr
    verify_connection: bool = True
    db_host: Text


class OLVaultPostgresDatabaseConfig(OLVaultDatabaseConfig):
    db_port: Int = DEFAULT_PORT_POSTGRES
    db_connection: Text = 'postgresql://{{db_user}}:{{db_pass}}@{{db_host}}:{{db_port}}/{{db_name}}'
    db_type: Text = DBEngines.postgres.value


class OLVaultMysqlDatabaseConfig(OLVaultDatabaseConfig):
    db_port: Int = DEFAULT_PORT_MYSQL
    db_connection: Text = '{{db_user}}:{{db_pass}}@tcp({{db_host}}:{{db_port}})/'
    db_type: Text = DBEngines.mysql_rds.value


class OLVaultDatabaseBackend(ComponentResource):

    def __init__(self, db_config: OLVaultDatabaseConfig, opts: ResourceOptions = None):
        super().__init__('ol:services:VaultDatabaseBackend', db_config.db_name, None, opts)

        resource_opts = ResourceOptions(parent=self).merge(opts)

        self.db_mount = Mount(
            f'{db_config.db_name}-mount-point',
            opts=resource_opts,
            path=db_config.mount_point,
            type='database'
        )
        database.SecretBackendConnection(
            f'{db_config.db_name}-database-connection',
            opts=resource_opts,
            backend=db_config.mount_point,
            verify_connection=db_config.verify_connection,
            name=db_config.db_name,
            data={
                'db_user': db_config.db_username,
                'db_pass': db_config.db_password,
                'db_host': db_config.db_host,
                'db_port': db_config.db_port,
                'db_name': db_config.db_name
            },
            **{db_config.db_type: db_config.db_connection}
        )

        database.SecretBackendRole(
            f'{db_config.db_name}-database-role-{role_name}',
            opts=resource_opts,
        )
