"""The complete state necessary to deploy an instance of the Redash application.

- Create an RDS PostgreSQL instance for storing Redash's configuration data and
  intermediate query results
- Mount a Vault database backend and provision role definitions for the Redash RDS
  database
- Create an IAM role for Redash instances to allow access to S3 and other AWS
  resources
- Create a Redis cluster in Elasticache
- Register a minion ID and key pair with the appropriate SaltStack master instance
- Provision a set of EC2 instances from a pre-built AMI with the configuration
  and code for Redash
- Provision an AWS load balancer and connect the deployed EC2 instances
- Create a DNS record for the deployed load balancer
"""
import json
from itertools import chain

from pulumi import Config, ResourceOptions, StackReference, export
from pulumi_aws import ec2, get_ami, get_caller_identity, iam, route53

from bridge.lib.magic_numbers import DEFAULT_POSTGRES_PORT
from ol_infrastructure.components.aws.cache import OLAmazonCache, OLAmazonRedisConfig
from ol_infrastructure.components.aws.database import OLAmazonDB, OLPostgresDBConfig
from ol_infrastructure.components.services.vault import (
    OLVaultDatabaseBackend,
    OLVaultPostgresDatabaseConfig,
)
from ol_infrastructure.lib.aws.ec2_helper import (
    DiskTypes,
    InstanceTypes,
    build_userdata,
)
from ol_infrastructure.lib.ol_types import AWSBase
from ol_infrastructure.lib.pulumi_helper import parse_stack
from ol_infrastructure.lib.stack_defaults import defaults
from ol_infrastructure.providers.salt.minion import (
    OLSaltStackMinion,
    OLSaltStackMinionInputs,
)

# TODO:
# - create load balancer for web nodes

redash_config = Config("redash")
salt_config = Config("saltstack")
stack_info = parse_stack()
network_stack = StackReference(f"infrastructure.aws.network.{stack_info.name}")
dns_stack = StackReference("infrastructure.aws.dns")
policy_stack = StackReference("infrastructure.aws.policies")
mitodl_zone_id = dns_stack.require_output("odl_zone_id")
data_vpc = network_stack.require_output("data_vpc")
operations_vpc = network_stack.require_output("operations_vpc")
redash_environment = f"data-{stack_info.env_suffix}"
aws_config = AWSBase(
    tags={"OU": "data", "Environment": redash_environment},
)

# Configure IAM and security settings for Redash instances
redash_instance_role = iam.Role(
    f"redash-instance-role-{stack_info.env_suffix}",
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
    path="/ol-data/redash-role/",
    tags=aws_config.tags,
)

iam.RolePolicyAttachment(
    f"redash-role-policy-{redash_environment}",
    policy_arn=policy_stack.require_output("iam_policies")["describe_instances"],
    role=redash_instance_role.name,
)

redash_instance_profile = iam.InstanceProfile(
    f"redash-instance-profile-{stack_info.env_suffix}",
    role=redash_instance_role.name,
    path="/ol-data/redash-profile/",
)

redash_instance_security_group = ec2.SecurityGroup(
    f"redash-instance-{stack_info.env_suffix}",
    description="Security group to assign to Redash application to control "
    "inter-service access",
    tags=aws_config.merged_tags({"Name": f"redash-instance-{redash_environment}"}),
    vpc_id=data_vpc["id"],
)

# Set up Postgres instance for Redash in RDS
redash_db_security_group = ec2.SecurityGroup(
    f"redash-db-access-{redash_environment}",
    description="Access from the data VPC to the Redash database",
    ingress=[
        ec2.SecurityGroupIngressArgs(
            protocol="tcp",
            from_port=DEFAULT_POSTGRES_PORT,
            to_port=DEFAULT_POSTGRES_PORT,
            security_groups=[redash_instance_security_group.id],
            description="PostgreSQL access from Redash instances",
        ),
        ec2.SecurityGroupIngressArgs(
            protocol="tcp",
            from_port=DEFAULT_POSTGRES_PORT,
            to_port=DEFAULT_POSTGRES_PORT,
            cidr_blocks=[operations_vpc["cidr"]],
        ),
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
    tags=aws_config.merged_tags({"Name": f"redash-db-access-{redash_environment}"}),
    vpc_id=data_vpc["id"],
)

redash_db_config = OLPostgresDBConfig(
    instance_name=f"redash-db-{redash_environment}",
    password=redash_config.require("db_password"),
    subnet_group_name=data_vpc["rds_subnet"],
    security_groups=[redash_db_security_group],
    tags=aws_config.tags,
    db_name="redash",
    **defaults(stack_info)["rds"],
)
redash_db = OLAmazonDB(redash_db_config)

redash_db_vault_backend_config = OLVaultPostgresDatabaseConfig(
    db_name=redash_db_config.db_name,
    mount_point=f"{redash_db_config.engine}-redash-{redash_environment}",
    db_admin_username=redash_db_config.username,
    db_admin_password=redash_config.require("db_password"),
    db_host=redash_db.db_instance.address,
)
redash_db_vault_backend = OLVaultDatabaseBackend(redash_db_vault_backend_config)

# Set up Redis instance for Redash in Elasticache
redis_config = Config("redis")
redash_redis_config = OLAmazonRedisConfig(
    encrypt_transit=True,
    auth_token=redis_config.require("auth_token"),
    engine_version="6.x",
    num_instances=3,
    shard_count=1,
    auto_upgrade=True,
    cluster_description="Redis cluster for Redash tasks and caching",
    cluster_name=f"redash-redis-{redash_environment}",
    security_groups=["dummy-group"],
    subnet_group=data_vpc[
        "elasticache_subnet"
    ],  # the name of the subnet group created in the OLVPC component resource
    tags=aws_config.tags,
    **defaults(stack_info)["redis"],
)

redis_cluster_security_group = ec2.SecurityGroup(
    f"redash-redis-cluster-{redash_environment}",
    name=f"redash-redis-{redash_environment}",
    description="Grant access to Redis from Redash",
    ingress=[
        ec2.SecurityGroupIngressArgs(
            from_port=redash_redis_config.port,
            to_port=redash_redis_config.port,
            protocol="tcp",
            security_groups=redash_instance_security_group.id.apply(
                lambda sec_group: [sec_group]
            ),
            description="Redis protocol communication",
        )
    ],
    tags=aws_config.merged_tags({"Name": f"redash-redis-{redash_environment}"}),
    vpc_id=data_vpc["id"],
)

redash_redis_config.security_groups = [redis_cluster_security_group.id]
redash_redis_cluster = OLAmazonCache(redash_redis_config)

# Deploy Redash instances on EC2
instance_type_name = (
    redash_config.get("instance_type") or InstanceTypes.burstable_medium.name
)
instance_type = InstanceTypes[instance_type_name].value
redash_instances = []
redash_export = {}
subnets = data_vpc["subnet_ids"]
subnet_id = subnets.apply(chain)
redash_ami = get_ami(
    filters=[
        {
            "name": "tag:Name",
            "values": ["redash"],
        },
        {
            "name": "virtualization-type",
            "values": ["hvm"],
        },
    ],
    most_recent=True,
    owners=[str(get_caller_identity().account_id)],
)
for count, subnet in zip(range(redash_config.get_int("instance_count") or 3), subnets):  # type: ignore # noqa: WPS221
    instance_name = f"redash-{redash_environment}-{count}"
    salt_minion = OLSaltStackMinion(
        f"saltstack-minion-{instance_name}",
        OLSaltStackMinionInputs(minion_id=instance_name),
    )

    cloud_init_userdata = build_userdata(
        instance_name=instance_name,
        minion_keys=salt_minion,
        minion_roles=["redash"],
        minion_environment=redash_environment,
        salt_host=f"salt-{stack_info.env_suffix}.private.odl.mit.edu",
    )

    instance_tags = aws_config.merged_tags(
        {
            "Name": instance_name,
        }
    )
    redash_instance = ec2.Instance(
        f"redash-instance-{redash_environment}-{count}",
        ami=redash_ami.id,
        user_data=cloud_init_userdata,
        instance_type=instance_type,
        iam_instance_profile=redash_instance_profile.id,
        tags=instance_tags,
        volume_tags=instance_tags,
        subnet_id=subnet,
        key_name=salt_config.require("key_name"),
        root_block_device=ec2.InstanceRootBlockDeviceArgs(
            volume_type=DiskTypes.ssd, volume_size=50
        ),
        vpc_security_group_ids=[
            data_vpc["security_groups"]["default"],
            data_vpc["security_groups"]["salt_minion"],
            data_vpc["security_groups"]["web"],
            redash_instance_security_group.id,
        ],
        opts=ResourceOptions(depends_on=[salt_minion]),
    )
    redash_instances.append(redash_instance)

    redash_export[instance_name] = {
        "public_ip": redash_instance.public_ip,
        "private_ip": redash_instance.private_ip,
        "ipv6_address": redash_instance.ipv6_addresses,
    }

fifteen_minutes = 60 * 15
redash_domain = route53.Record(
    f"redash-{stack_info.env_suffix}-service-domain",
    name=redash_config.require("domain"),
    type="A",
    ttl=fifteen_minutes,
    records=[instance["public_ip"] for instance in redash_export.values()],
    zone_id=mitodl_zone_id,
    opts=ResourceOptions(depends_on=[redash_instance]),
)
redash_domain_v6 = route53.Record(
    f"redash-{stack_info.env_suffix}-service-domain-v6",
    name=redash_config.require("domain"),
    type="AAAA",
    ttl=fifteen_minutes,
    records=[instance["ipv6_address"][0] for instance in redash_export.values()],
    zone_id=mitodl_zone_id,
    opts=ResourceOptions(depends_on=[redash_instance]),
)

export(
    "redash_app",
    {
        "rds_host": redash_db.db_instance.address,
        "redis_cluster": redash_redis_cluster.cache_cluster.primary_endpoint_address,
        "instances": redash_export,
    },
)
