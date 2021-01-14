"""Create the infrastructure and services needed to support the OCW Studio application.

- Create a Redis instance in AWS Elasticache
- Create a PostgreSQL database in AWS RDS for production environments
- Create an IAM policy to grant access to S3 and other resources
"""
from pulumi import Config, StackReference, export
from pulumi_aws import ec2

from ol_infrastructure.components.aws.cache import OLAmazonCache, OLAmazonRedisConfig
from ol_infrastructure.components.aws.database import OLAmazonDB, OLPostgresDBConfig
from ol_infrastructure.components.services.vault import (
    OLVaultDatabaseBackend,
    OLVaultPostgresDatabaseConfig,
)
from ol_infrastructure.lib.ol_types import AWSBase
from ol_infrastructure.lib.pulumi_helper import parse_stack
from ol_infrastructure.lib.stack_defaults import defaults

ocw_studio_config = Config("ocw_studio")
stack_info = parse_stack()
network_stack = StackReference(f"infrastructure.aws.network.{stack_info.name}")
apps_vpc = network_stack.require_output("applications_vpc")
operations_vpc = network_stack.require_output("operations_vpc")
aws_config = AWSBase(
    tags={
        "OU": "open-courseware",
        "Environment": f"applications_{stack_info.env_suffix}",
    }
)

# Create RDS instance
ocw_studio_db_security_group = ec2.SecurityGroup(
    f"ocw-studio-db-access-{stack_info.env_suffix}",
    description=f"Access control for the OCW Studio DB in {stack_info.name}",
    ingress=[
        ec2.SecurityGroupIngressArgs(
            protocol="tcp",
            from_port=5432,  # noqa: WPS432
            to_port=5432,  # noqa: WPS432
            cidr_blocks=["0.0.0.0/0"],
            ipv6_cidr_blocks=["::/0"],
            description="Allow access over the public internet from Heroku",
        )
    ],
    egress=[
        ec2.SecurityGroupEgressArgs(
            from_port=0,
            to_port=0,
            protocol="-1",
            cidr_blocks=["0.0.0.0/0"],
            ipv6_cidr_blocks=["::/0"],
        )
    ],
    tags=aws_config.merged_tags(
        {"name": "ocw-studio-db-access-applications-{stack_info.env_suffix}"}
    ),
    vpc_id=apps_vpc["id"],
)

ocw_studio_db_config = OLPostgresDBConfig(
    instance_name=f"ocw-studio-db-applications-{stack_info.env_suffix}",
    password=ocw_studio_config.require("db_password"),
    subnet_group_name=apps_vpc["rds_subnet"],
    security_groups=[ocw_studio_db_security_group],
    tags=aws_config.tags,
    db_name="ocw_studio",
    public_access=True,
    **defaults(stack_info)["rds"],
)
ocw_studio_db = OLAmazonDB(ocw_studio_db_config)

ocw_studio_vault_backend_config = OLVaultPostgresDatabaseConfig(
    db_name=ocw_studio_db_config.db_name,
    mount_point=f"{ocw_studio_db_config.engine}-ocw-studio-applications-{stack_info.env_suffix}",
    db_admin_username=ocw_studio_db_config.username,
    db_admin_password=ocw_studio_db_config.password.get_secret_value(),
    db_host=ocw_studio_db.db_instance.address,
)
ocw_studio_vault_backend = OLVaultDatabaseBackend(ocw_studio_vault_backend_config)


export("ocw_studio_app", {"rds_host": ocw_studio_db.db_instance.address})
