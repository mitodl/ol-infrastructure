import pulumi
import pulumi_aws as aws
from pulumi_aws import Provider

from bridge.lib.magic_numbers import AWS_RDS_DEFAULT_DATABASE_CAPACITY
from ol_infrastructure.components.aws.database import OLAmazonDB, OLPostgresDBConfig
from ol_infrastructure.components.services.vault import (
    OLVaultDatabaseBackend,
    OLVaultPostgresDatabaseConfig,
)
from ol_infrastructure.lib.pulumi_helper import parse_stack
from ol_infrastructure.lib.stack_defaults import defaults

stack_info = parse_stack()
config = pulumi.Config("ol-infrastructure-bootcamps")
region = config.get("region")
provider = Provider("provider", region=region)
env = config.get("env")
app = config.get("app")
hosted_zone_id = config.get("hosted_zone_id")
rds_subnet_group_name = config.get("rds_subnet_groups")
# network_stack = StackReference(f"infrastructure.aws.network.{stack_info.name}")
# target_vpc_name = config.get("target_vpc") or f"{stack_info.env_prefix}_vpc"
# target_vpc = network_stack.require_output(target_vpc_name)
ol_bootcamps_app = aws.s3.Bucket(
    f"ol_{app}_app_{env}",
    arn=f"arn:aws:s3:::ol-{app}-app-{env}",
    bucket=f"ol-{app}-app-{env}",
    hosted_zone_id=f"{hosted_zone_id}",
    request_payer="BucketOwner",
    tags={
        "Department": f"{app}",
        "Environment": f"{env}",
        "OU": f"{app}",
        "business_unit": f"{app}",
    },
    versioning=aws.s3.BucketVersioningArgs(
        enabled=True,
    ),
    opts=pulumi.ResourceOptions(protect=True),
)
if stack_info.env_suffix == "production":
    rds_defaults = defaults(stack_info)["rds"]
    rds_password = config.require("rds_password")
    default_security_group_id = config.get("default_security_group_id")
    vault_security_group_id = config.get("vault_security_group_id")
    rds_security_group_id = config.get("rds_security_group_id")

    rds_bootcamps = aws.ec2.SecurityGroup.get(
        resource_name=f"rds_{app}_{env}", id=rds_security_group_id
    )
    vault_bootcamps = aws.ec2.SecurityGroup.get(
        resource_name=f"vault_{app}_{env}", id=vault_security_group_id
    )
    default_sg = aws.ec2.SecurityGroup.get(
        resource_name=f"default_{app}_{env}", id=default_security_group_id
    )

    bootcamp_db_config = OLPostgresDBConfig(
        instance_name=f"{app}-rds-postgresql",
        password=rds_password,
        storage=config.get("db_capacity") or str(AWS_RDS_DEFAULT_DATABASE_CAPACITY),
        subnet_group_name=rds_subnet_group_name,
        parameter_overrides=[{"name": "rds.force_ssl", "value": 0}],
        tags={
            "Department": f"{app}",
            "Environment": f"{env}-apps",
            "Name": f"{app}-rds-postgresql",
            "OU": f"{app}",
            "Purpose": f"{app}",
            "business_unit": f"{app}",
        },
        db_name="bootcamp_ecommerce",
        engine_version="12.7",
        security_groups=[rds_bootcamps, vault_bootcamps, default_sg],
        **rds_defaults,
    )
    bootcamp = OLAmazonDB(bootcamp_db_config)
    bootcamps_vault_backend_config = OLVaultPostgresDatabaseConfig(
        db_name=bootcamp_db_config.db_name,
        db_admin_username=bootcamp_db_config.username,
        mount_point=f"{bootcamp_db_config.engine}-{app}-{stack_info.env_suffix}",
        db_admin_password=bootcamp_db_config.password.get_secret_value(),
        db_host=bootcamp_db_config.db_instance.address,
        **defaults(stack_info)["rds"],
    )
    bootcamps_vault_backend = OLVaultDatabaseBackend(bootcamps_vault_backend_config)
