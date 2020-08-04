"""
This module controls mounting and configuration of database secret backends in Vault.

This includes:
- Mount a database backend at the configured mount point
- Set the configuration according to the requirements of the backend type
- Control the lease TTL settings
- Define the base set of roles according to our established best practices
"""
from enum import Enum
from typing import Text

from pulumi import ComponentResource, ResourceOptions
from pulumi_vault import Mount, database
from pydantic import BaseModel, SecretStr

from ol_infrastructure.lib.vault import (
    mysql_sql_statements,
    postgres_sql_statements
)

DEFAULT_PORT_POSTGRES = 5432
DEFAULT_PORT_MYSQL = 3306
DEFAULT_POT_MONGODB = 27017
SIX_MONTHS = 60 * 60 * 24 * 30 * 6

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
    max_ttl: int = SIX_MONTHS
    default_ttl: int = SIX_MONTHS


class OLVaultPostgresDatabaseConfig(OLVaultDatabaseConfig):
    db_port: Int = DEFAULT_PORT_POSTGRES
    db_connection: Text = 'postgresql://{{db_user}}:{{db_pass}}@{{db_host}}:{{db_port}}/{{db_name}}'
    db_type: Text = DBEngines.postgres.value
    role_statements: Dict[Text, Text] = postgresql_sql_statements


class OLVaultMysqlDatabaseConfig(OLVaultDatabaseConfig):
    db_port: Int = DEFAULT_PORT_MYSQL
    db_connection: Text = '{{db_user}}:{{db_pass}}@tcp({{db_host}}:{{db_port}})/'
    db_type: Text = DBEngines.mysql_rds.value
    role_statements: Dict[Text, Text] = mysql_sql_statements


class OLVaultDatabaseBackend(ComponentResource):

    def __init__(self, db_config: OLVaultDatabaseConfig, opts: ResourceOptions = None):
        super().__init__('ol:services:VaultDatabaseBackend', db_config.db_name, None, opts)

        resource_opts = ResourceOptions(parent=self).merge(opts)

        self.db_mount = Mount(
            f'{db_config.db_name}-mount-point',
            opts=resource_opts,
            path=db_config.mount_point,
            type='database',
            max_lease_ttl_seconds=db_config.max_ttl,
            default_lease_ttl_seconds=db_config.default_ttl
        )
        self.db_connection = database.SecretBackendConnection(
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

        self.db_roles = {}
        for role_name, role_defs in db_config.role_statements.items():
            self.db_roles[role_name] = database.SecretBackendRole(
                f'{db_config.db_name}-database-role-{role_name}',
                opts=resource_opts,
                name=role_name,
                backend=db_config.mount_point,
                db_name=db_config.db_name,
                creation_statements=role_defs['create'].format(role_name=db_config.db_name),
                revocation_statements=role_defs['revoke'].format(role_name=db_config.db_name),
                max_ttl=db_config.max_ttl,
                default_ttl=db_config.default_ttl
            )
        self.register_outputs({})
