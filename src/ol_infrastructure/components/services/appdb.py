"""
This module collapses as much boilerplate as possible to facilitate the setup
of an application's database.

This is meant to be used as the standard, normal, run-of-the-mill database
configuration for a typical application. Special snowflakes should continue
to use the OLDatabase components available in `aws/database.py`.
"""

from typing import Optional

from pulumi import ComponentResource, Output, ResourceOptions, StackReference
from pulumi_aws import ec2
from pydantic import BaseModel, ConfigDict, field_validator

from bridge.lib.magic_numbers import (
    AWS_RDS_DEFAULT_DATABASE_CAPACITY,
    DEFAULT_POSTGRES_PORT,
)
from ol_infrastructure.components.aws.database import (
    OLAmazonDB,
    OLPostgresDBConfig,
    SecretStr,
)
from ol_infrastructure.components.services.vault import (
    OLVaultDatabaseBackend,
    OLVaultPostgresDatabaseConfig,
)
from ol_infrastructure.lib.ol_types import AWSBase
from ol_infrastructure.lib.pulumi_helper import parse_stack
from ol_infrastructure.lib.stack_defaults import defaults

stack_info = parse_stack()
env_name = f"{stack_info.env_prefix}-{stack_info.env_suffix}"
# Network and vault stack references are defined in-situ for uniqueness.


class OLAppDatabaseConfig(BaseModel):
    """Configuration for the MIT OL Database component"""

    model_config = ConfigDict(arbitrary_types_allowed=True)
    app_name: str
    app_security_group: ec2.SecurityGroup
    app_ou: str
    app_db_name: str
    app_db_password: str
    app_db_instance_size: str | None = None
    app_db_capacity: int | None = None
    app_vpc_id: Output[str]
    target_vpc_name: str

    @field_validator("target_vpc_name")
    @classmethod
    def validate_target_vpc_name(cls, target_vpc_name: str) -> str:
        if not target_vpc_name.endswith("_vpc"):
            target_vpc_name += "_vpc"
        return target_vpc_name


class OLAppDatabase(ComponentResource):
    """
    MIT OL Database component
    """

    def __init__(
        self,
        ol_db_config: OLAppDatabaseConfig,
        opts: Optional[ResourceOptions] = None,
    ):
        """
        Create all the resources necessary for an MIT OL Postgres Database.
        :param ol_db_config: Configuration object for database class.
        :type OLAppDatabase

        :param opts: Pulumi resource options
        :type opts: ResourceOptions

        :rtype: OLAppDatabase
        """
        self.ol_db_config = ol_db_config
        self.aws_config: AWSBase = AWSBase(
            tags={
                "OU": self.ol_db_config.app_ou,
                "Environment": f"{env_name}",
            },
        )
        super().__init__(
            "ol:infrastructure:components:services:OLAppDatabase",
            self.ol_db_config.app_name,
            None,
            opts=opts,
        )

        # We do this here rather than at the top because we need a unique
        # identifier for the name.
        db_network_stack = StackReference(
            name=f"db_network_stack_reference_{self.ol_db_config.app_db_name}",
            stack_name=f"infrastructure.aws.network.{stack_info.name}",
        )

        vault_stack = StackReference(
            name=f"db_vault_stack_reference_{self.ol_db_config.app_db_name}",
            stack_name=f"infrastructure.vault.operations.{stack_info.name}",
        )

        target_vpc = db_network_stack.require_output(ol_db_config.target_vpc_name)

        ################################################
        # RDS configuration and networking setup
        # NEEDS FIX -CAP
        self.db_security_group: ec2.SecurityGroup = ec2.SecurityGroup(
            f"learn-ai-db-security-group-{stack_info.env_suffix}",
            name=f"learn-ai-db-security-group-{stack_info.env_suffix}",
            description="Access control for the learn-ai database.",
            ingress=[
                ec2.SecurityGroupIngressArgs(
                    security_groups=[
                        vault_stack.require_output("vault_server")["security_group"],
                    ],
                    protocol="tcp",
                    from_port=DEFAULT_POSTGRES_PORT,
                    to_port=DEFAULT_POSTGRES_PORT,
                    description="Access to postgres from consul and vault.",
                ),
                ec2.SecurityGroupIngressArgs(
                    security_groups=[self.ol_db_config.app_security_group],
                    protocol="tcp",
                    from_port=DEFAULT_POSTGRES_PORT,
                    to_port=DEFAULT_POSTGRES_PORT,
                    description="Allow application pods to talk to DB",
                ),
            ],
            vpc_id=self.ol_db_config.app_vpc_id,
            tags=self.aws_config.tags,
            opts=ResourceOptions(parent=self),
        )

        self.app_db_config = OLPostgresDBConfig(
            instance_name=f"{ol_db_config.app_name}-db-{stack_info.env_suffix}",
            password=SecretStr(self.ol_db_config.app_db_password),
            subnet_group_name=target_vpc["rds_subnet"],
            security_groups=[self.db_security_group],
            storage=self.ol_db_config.app_db_capacity
            or AWS_RDS_DEFAULT_DATABASE_CAPACITY,
            engine_major_version="16",
            tags=self.aws_config.tags,
            db_name=self.ol_db_config.app_db_name,
            **defaults(stack_info)["rds"],
        )
        self.app_db = OLAmazonDB(
            self.app_db_config,
            opts=ResourceOptions(parent=self),
        )

        self.app_db_vault_backend_config = OLVaultPostgresDatabaseConfig(
            db_name=self.ol_db_config.app_db_name,
            mount_point=f"{self.app_db_config.engine}-{ol_db_config.app_name}",
            db_admin_username=self.app_db_config.username,
            db_admin_password=self.ol_db_config.app_db_password,
            db_host=self.app_db.db_instance.address,
        )
        self.app_db_vault_backend = OLVaultDatabaseBackend(
            self.app_db_vault_backend_config,
            opts=ResourceOptions(
                delete_before_replace=True,
                parent=self.app_db,
            ),
        )
