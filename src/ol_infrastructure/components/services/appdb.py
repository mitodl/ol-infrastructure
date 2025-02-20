"""
This module collapses as much boilerplate as possible to facilitate the setup
of an application's database.
"""

from pulumi import ComponentResource, StackReference
from pulumi_aws import ec2

from bridge.lib.magic_numbers import (
    AWS_RDS_DEFAULT_DATABASE_CAPACITY,
    DEFAULT_POSTGRES_PORT,
)
from ol_infrastructure.components.aws.database import (
    OLAmazonDB,
    OLPostgresDBConfig,
    SecretStr,
)
from ol_infrastructure.lib.ol_types import AWSBase
from ol_infrastructure.lib.pulumi_helper import parse_stack
from ol_infrastructure.lib.stack_defaults import defaults

stack_info = parse_stack()
env_name = f"{stack_info.env_prefix}-{stack_info.env_suffix}"
vault_stack = StackReference(f"infrastructure.vault.operations.{stack_info.name}")
network_stack = StackReference(f"infrastructure.aws.network.{stack_info.name}")
aws_config = AWSBase(
    tags={
        "OU": "applications",
        "Environment": f"{env_name}",
    }
)


class OLDatabaseConfig:
    """Confugration for the MIT OL Database component"""

    app_name: str
    app_db_name: str
    app_db_password: str
    app_db_instance_size: str | None
    app_db_capacity: str | None
    target_vpc_name: str
    app_security_group: ec2.SecurityGroup


class OLDatabase(ComponentResource):
    """MIT OL Database component"""

    def __init__( # noqa: D107
            self,
            ol_db_config: OLDatabaseConfig
        ):

        self.ol_db_config = ol_db_config
        super().__init__(
            "ol:infrastructure:aws:OLDatabase", ol_db_config.app_name, None
        )

        target_vpc = network_stack.require_output(ol_db_config.target_vpc_name)
        self.app_db_security_group = ec2.SecurityGroup(
            f"{ol_db_config.app_name}-db-security-group-{stack_info.env_suffix}",
            name=f"{ol_db_config.app_name}-db-security-group-{stack_info.env_suffix}",
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
                    security_groups=[self.ol_db_config.app_security_group.id],
                    protocol="tcp",
                    from_port=DEFAULT_POSTGRES_PORT,
                    to_port=DEFAULT_POSTGRES_PORT,
                    description="Allow application pods to talk to DB",
                ),
            ],
            vpc_id=target_vpc["id"],
            tags=aws_config.tags,
        )

        rds_defaults = defaults(stack_info)["rds"]
        rds_defaults["instance_size"] = (
            self.ol_db_config.app_db_instance_size or rds_defaults["instance_size"]
        )

        self.app_db_config = OLPostgresDBConfig(
            instance_name=f"{ol_db_config.app_name}-db-{stack_info.env_suffix}",
            password=SecretStr(self.ol_db_config.app_db_password),
            subnet_group_name=target_vpc["rds_subnet"],
            security_groups=[self.app_db_security_group],
            storage=self.ol_db_config.app_db_capacity or str(AWS_RDS_DEFAULT_DATABASE_CAPACITY),
            engine_major_version="16",
            tags=aws_config.tags,
            db_name=self.ol_db_config.app_db_name,
            **defaults(stack_info)["rds"],
        )
        self.app_db_vault_backend_config = OLVaultPostgresDatabaseConfig(
            db_name=self.app_db_config.db_name,
            mount_point=f"{self.app_db_config.engine}-learn-ai",
            db_admin_username=self.app_db_config.username,
            db_admin_password=self.app_db_config.db_password,
            db_host=app_db.db_instance.address,
        )
        app_db_vault_backend = OLVaultDatabaseBackend(
            self.app_db_vault_backend_config,
        )

        self.app_db = OLAmazonDB(app_db_config)
