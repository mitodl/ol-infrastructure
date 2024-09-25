import json
from pathlib import Path

import pulumi_vault as vault
from pulumi import Config, StackReference
from pulumi_aws import ec2, get_caller_identity, iam
from pulumi_consul import Node, Service, ServiceCheckArgs

from bridge.lib.magic_numbers import (
    AWS_RDS_DEFAULT_DATABASE_CAPACITY,
    DEFAULT_POSTGRES_PORT,
)
from ol_infrastructure.components.aws.database import OLAmazonDB, OLPostgresDBConfig
from ol_infrastructure.components.services.vault import (
    OLVaultDatabaseBackend,
    OLVaultPostgresDatabaseConfig,
)
from ol_infrastructure.lib.aws.ec2_helper import default_egress_args
from ol_infrastructure.lib.aws.rds_helper import DBInstanceTypes
from ol_infrastructure.lib.consul import get_consul_provider
from ol_infrastructure.lib.ol_types import AWSBase
from ol_infrastructure.lib.pulumi_helper import parse_stack
from ol_infrastructure.lib.stack_defaults import defaults
from ol_infrastructure.lib.vault import setup_vault_provider

setup_vault_provider()
stack_info = parse_stack()

open_metadata_config = Config("open_metadata")
dns_stack = StackReference("infrastructure.aws.dns")
network_stack = StackReference(f"infrastructure.aws.network.{stack_info.name}")
policy_stack = StackReference("infrastructure.aws.policies")
vault_stack = StackReference(f"infrastructure.vault.operations.{stack_info.name}")
consul_stack = StackReference(f"infrastructure.consul.operations.{stack_info.name}")
mitodl_zone_id = dns_stack.require_output("odl_zone_id")
apps_vpc = network_stack.require_output("applications_vpc")
open_metadata_environment = f"operations-{stack_info.env_suffix}"
aws_config = AWSBase(
    tags={"OU": "operations", "Environment": open_metadata_environment},
)
consul_provider = get_consul_provider(stack_info)

consul_security_groups = consul_stack.require_output("security_groups")
aws_account = get_caller_identity()

open_metadata_role = iam.Role(
    "open-metadata-poc-instance-role",
    assume_role_policy=json.dumps(
        {
            "Version": "2012-10-17",
            "Statement": {
                "Effect": "Allow",
                "Action": "sts:AssumeRole",
                "Principal": {"Service": "ec2.amazonaws.com"},
            },
        }
    ),
    name=f"open-metadata-instance-role-{stack_info.env_suffix}",
    path="/ol-infrastructure/open-metadata-role/",
    tags=aws_config.tags,
)

open_metadata_server_security_group = ec2.SecurityGroup(
    f"open-metadata-server-security-group-{stack_info.env_suffix}",
    name=f"open-metadata-server-security-group-{stack_info.env_suffix}",
    ingress=[
        # ec2.SecurityGroupIngressArgs(
        # ),
        ec2.SecurityGroupIngressArgs(
            protocol="tcp",
            from_port=8011,
            to_port=8011,
            cidr_blocks=["0.0.0.0/0"],
            description="Allow mike and matt to connect to http.",
        ),
    ],
    egress=default_egress_args,
    vpc_id=apps_vpc["id"],
)

open_metadata_database_security_group = ec2.SecurityGroup(
    f"open-metadata-database-security-group-{stack_info.env_suffix}",
    name=f"open-metadata-database-security-group-{stack_info.env_suffix}",
    description="Access control for the open metadata database.",
    ingress=[
        ec2.SecurityGroupIngressArgs(
            security_groups=[
                open_metadata_server_security_group.id,
                consul_security_groups["consul_server"],
                vault_stack.require_output("vault_server")["security_group"],
            ],
            protocol="tcp",
            from_port=DEFAULT_POSTGRES_PORT,
            to_port=DEFAULT_POSTGRES_PORT,
            description="Access to postgres from open metadata servers.",
        ),
        ec2.SecurityGroupIngressArgs(
            security_groups=[],
            protocol="tcp",
            from_port=DEFAULT_POSTGRES_PORT,
            to_port=DEFAULT_POSTGRES_PORT,
            cidr_blocks=["73.218.126.92/32", "98.110.175.231/32"],
            description="Allow mike and matt to connect to postgres.",
        ),
    ],
    vpc_id=apps_vpc["id"],
    tags=aws_config.tags,
)

rds_defaults = defaults(stack_info)["rds"]
rds_defaults["instance_size"] = (
    open_metadata_config.get("db_instance_size") or DBInstanceTypes.small.value
)

open_metadata_db_config = OLPostgresDBConfig(
    instance_name=f"open-metadata-db-{stack_info.env_suffix}",
    password=open_metadata_config.get("db_password"),
    subnet_group_name=apps_vpc["rds_subnet"],
    security_groups=[open_metadata_database_security_group],
    storage=open_metadata_config.get("db_capacity")
    or str(AWS_RDS_DEFAULT_DATABASE_CAPACITY),
    engine_major_version="15",
    tags=aws_config.tags,
    db_name="open_metadata",
    **defaults(stack_info)["rds"],
)
open_metadata_db = OLAmazonDB(open_metadata_db_config)

open_metadata_db_vault_backend_config = OLVaultPostgresDatabaseConfig(
    db_name=open_metadata_db_config.db_name,
    mount_point=f"{open_metadata_db_config.engine}-open-metadata",
    db_admin_username=open_metadata_db_config.username,
    db_admin_password=open_metadata_config.get("db_password"),
    db_host=open_metadata_db.db_instance.address,
)
open_metadata_db_vault_backend = OLVaultDatabaseBackend(
    open_metadata_db_vault_backend_config
)

open_metadata_db_consul_node = Node(
    "open-metadata-postgres-db",
    name="open-metadata-postgres-db",
    address=open_metadata_db.db_instance.address,
    opts=consul_provider,
)

open_metadata_db_consul_service = Service(
    "open-metadata-instance-db-service",
    node=open_metadata_db_consul_node.name,
    name="open-metadata-db",
    port=open_metadata_db_config.port,
    meta={
        "external-node": True,
        "external-probe": True,
    },
    checks=[
        ServiceCheckArgs(
            check_id="open-metadata-instance-db",
            interval="10s",
            name="open-metadata-instance-id",
            timeout="60s",
            status="passing",
            tcp=open_metadata_db.db_instance.address.apply(
                lambda address: f"{address}:{open_metadata_db_config.port}"
            ),
        )
    ],
    opts=consul_provider,
)

# Create a vault policy to allow dagster to get to the secrets it needs
open_metadata_server_vault_policy = vault.Policy(
    "open-metadata-server-vault-policy",
    name="open-metadata-server",
    policy=Path(__file__)
    .parent.joinpath("open_metadata_server_policy.hcl")
    .read_text(),
)
## Begin block for migrating to pyinfra images
consul_datacenter = consul_stack.require_output("datacenter")
