"""
This module collapses as much boilerplate as possible to facilitate the setup
of an application's database.

This is meant to be used as the standard, normal, run-of-the-mill database
configuration for a typical application. Special snowflakes should continue
to use the OLDatabase components available in `aws/database.py`.
"""

from enum import StrEnum
from typing import Any

from pulumi import Alias, ComponentResource, Output, ResourceOptions, StackReference
from pulumi_aws import ec2
from pydantic import BaseModel, ConfigDict

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
# Network and vault stack references are defined in-situ for uniqueness.


class AliasKey(StrEnum):
    secgroup = "secgroup"
    db = "database"
    vault = "vault"


class OLAppDatabaseConfig(BaseModel):
    """Configuration for the MIT OL Database component"""

    model_config = ConfigDict(arbitrary_types_allowed=True)
    app_name: str
    app_security_group: ec2.SecurityGroup
    app_db_name: str
    app_db_password: str
    app_db_instance_size: str | None = None
    app_db_capacity: int | None = None
    app_vpc: Output[dict[str, Any]]
    aws_config: AWSBase
    alias_map: dict[AliasKey, list[Alias]] | None = None


class OLAppDatabase(ComponentResource):
    """
    MIT OL Database component
    """

    def __init__(
        self,
        ol_db_config: OLAppDatabaseConfig,
        opts: ResourceOptions | None = None,
    ):
        """
        Create all the resources necessary for an MIT OL Postgres Database.
        :param ol_db_config: Configuration object for database class.
        :type OLAppDatabase

        :param opts: Pulumi resource options
        :type opts: ResourceOptions

        :rtype: OLAppDatabase
        """
        super().__init__(
            "ol:infrastructure:components:services:OLAppDatabase",
            ol_db_config.app_name,
            None,
            opts=opts,
        )

        network_stack = StackReference(
            name=f"db_network_stack_reference_{ol_db_config.app_db_name}",
            stack_name=f"infrastructure.aws.network.{stack_info.name}",
        )
        data_vpc = network_stack.require_output("data_vpc")
        vault_stack = StackReference(
            name=f"db_vault_stack_reference_{ol_db_config.app_db_name}",
            stack_name=f"infrastructure.vault.operations.{stack_info.name}",
        )

        consul_stack = StackReference(
            name=f"db_consul_stack_reference_{ol_db_config.app_db_name}",
            stack_name=f"infrastructure.consul.apps.{stack_info.name}",
        )

        ################################################
        # RDS configuration and networking setup
        self.db_security_group: ec2.SecurityGroup = ec2.SecurityGroup(
            f"{ol_db_config.app_name}-db-security-group-{stack_info.env_suffix}",
            name=f"{ol_db_config.app_name}-db-security-group-{stack_info.env_suffix}",
            description=f"Access control for the {ol_db_config.app_name} database.",
            ingress=[
                ec2.SecurityGroupIngressArgs(
                    security_groups=[
                        consul_stack.require_output("security_groups")["consul_server"],
                        vault_stack.require_output("vault_server")["security_group"],
                    ],
                    # Airbyte isn't using pod security groups in Kubernetes. This is a
                    # workaround to allow for data integration from the data Kubernetes
                    # cluster. (TMM 2025-05-16)
                    cidr_blocks=data_vpc["k8s_pod_subnet_cidrs"].apply(
                        lambda pod_cidrs: [*pod_cidrs]
                    ),
                    protocol="tcp",
                    from_port=DEFAULT_POSTGRES_PORT,
                    to_port=DEFAULT_POSTGRES_PORT,
                    description="Access to postgres from consul, airbyte, and vault.",
                ),
                ec2.SecurityGroupIngressArgs(
                    security_groups=[ol_db_config.app_security_group],
                    protocol="tcp",
                    from_port=DEFAULT_POSTGRES_PORT,
                    to_port=DEFAULT_POSTGRES_PORT,
                    description="Allow application pods to talk to DB",
                ),
            ],
            vpc_id=ol_db_config.app_vpc["id"],
            tags=ol_db_config.aws_config.tags,
            opts=ResourceOptions(
                ignore_changes=["description"],
                parent=self,
                aliases=(ol_db_config.alias_map or {}).get(AliasKey.secgroup, []),
            ),
        )

        self.app_db_config = OLPostgresDBConfig(
            instance_name=f"{ol_db_config.app_name}-db-{stack_info.env_suffix}",
            password=SecretStr(ol_db_config.app_db_password),
            subnet_group_name=ol_db_config.app_vpc["rds_subnet"],
            security_groups=[self.db_security_group],
            storage=ol_db_config.app_db_capacity or AWS_RDS_DEFAULT_DATABASE_CAPACITY,
            tags=ol_db_config.aws_config.tags,
            db_name=ol_db_config.app_db_name,
            **defaults(stack_info)["rds"],
        )
        self.app_db = OLAmazonDB(
            self.app_db_config,
            opts=ResourceOptions(
                parent=self,
                aliases=(ol_db_config.alias_map or {}).get(AliasKey.db, []),
            ),
        )

        app_db_vault_backend_config = OLVaultPostgresDatabaseConfig(
            db_name=ol_db_config.app_db_name,
            mount_point=f"{self.app_db_config.engine}-{ol_db_config.app_name}",
            db_admin_username=self.app_db_config.username,
            db_admin_password=ol_db_config.app_db_password,
            db_host=self.app_db.db_instance.address,
        )
        self.app_db_vault_backend = OLVaultDatabaseBackend(
            app_db_vault_backend_config,
            opts=ResourceOptions(
                delete_before_replace=True,
                parent=self.app_db,
                aliases=(ol_db_config.alias_map or {}).get(AliasKey.vault, []),
            ),
        )
