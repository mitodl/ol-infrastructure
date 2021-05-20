import json

from pulumi import Config
from pulumi.stack_reference import StackReference
from pulumi_aws import ec2, get_caller_identity, iam, s3
from pulumi_consul import Node, Service, ServiceCheckArgs

from bridge.lib.magic_numbers import DEFAULT_MYSQL_PORT, DEFAULT_REDIS_PORT
from ol_infrastructure.components.aws.cache import OLAmazonRedisConfig
from ol_infrastructure.components.aws.database import OLAmazonDB, OLMariaDBConfig
from ol_infrastructure.components.services.vault import (
    OLVaultDatabaseBackend,
    OLVaultMysqlDatabaseConfig,
)
from ol_infrastructure.lib.aws.ec2_helper import default_egress_args
from ol_infrastructure.lib.aws.iam_helper import IAM_POLICY_VERSION, lint_iam_policy
from ol_infrastructure.lib.ol_types import AWSBase
from ol_infrastructure.lib.pulumi_helper import parse_stack
from ol_infrastructure.lib.stack_defaults import defaults

mitxonline_config = Config("mitxonline")
stack_info = parse_stack()
network_stack = StackReference(f"infrastructure.aws.network.{stack_info.name}")
policy_stack = StackReference("infrastructure.aws.policies")
dns_stack = StackReference("infrastructure.aws.dns")
consul_stack = StackReference(f"infrastructure.consul.mitxonline.{stack_info.name}")
mitxonline_vpc = network_stack.require_output("mitxonline_vpc")
aws_config = AWSBase(
    tags={"OU": "mitxonline", "Environment": f"mitxonline-{stack_info.env_suffix}"}
)

aws_account = get_caller_identity()
mitxonline_vpc_id = mitxonline_vpc["id"]
env_name = f"mitxonline-{stack_info.env_suffix}"


##############
# S3 Buckets #
##############

storage_bucket_name = f"mitxonline-edxapp-storage-{stack_info.env_suffix}"
edxapp_storage_bucket = s3.Bucket(
    "mitxonline-edxapp-storage-bucket",
    bucket=storage_bucket_name,
    versioning=s3.BucketVersioningArgs(enabled=True),
    tags=aws_config.tags,
)

grades_bucket_name = f"mitxonline-edxapp-grades-{stack_info.env_suffix}"
edxapp_storage_bucket = s3.Bucket(
    "mitxonline-edxapp-grades-bucket",
    bucket=grades_bucket_name,
    versioning=s3.BucketVersioningArgs(enabled=True),
    tags=aws_config.tags,
)

########################
# IAM Roles & Policies #
########################

parliament_config = {
    "PERMISSIONS_MANAGEMENT_ACTIONS": {
        "ignore_locations": [{"actions": ["s3:putobjectacl"]}]
    }
}

edx_platform_policy_document = {
    "Version": IAM_POLICY_VERSION,
    "Statement": [
        {
            "Effect": "Allow",
            "Action": "s3:ListAllMyBuckets",
            "Resource": "*",
        },
        {
            "Effect": "Allow",
            "Action": [
                "s3:GetObject",
                "s3:PutObject",
                "s3:PutObjectAcl",
                "s3:DeleteObject",
                "s3:ListBucket*",
            ],
            "Resource": [
                f"arn:aws:s3:::{storage_bucket_name}",
                f"arn:aws:s3:::{storage_bucket_name}/*",
                f"arn:aws:s3:::{grades_bucket_name}",
                f"arn:aws:s3:::{grades_bucket_name}/*",
            ],
        },
    ],
}

edx_platform_policy = iam.Policy(
    "edx-platform-policy",
    name_prefix="edx-platform-policy-",
    path=f"/ol-applications/edx-platform/mitxonline/{stack_info.env_suffix}/",
    policy=lint_iam_policy(
        edx_platform_policy_document,
        stringify=True,
        parliament_config=parliament_config,
    ),
    description="AWS access permissions for edX application instances",
)
edx_platform_iam_role = iam.Role(
    "edx-platform-instance-role",
    assume_role_policy=json.dumps(
        {
            "Version": IAM_POLICY_VERSION,
            "Statement": {
                "Effect": "Allow",
                "Action": "sts:AssumeRole",
                "Principal": {"Service": "ec2.amazonaws.com"},
            },
        }
    ),
    name_prefix="edx-platform-role-",
    path=f"/ol-applications/edx-platform/mitxonline/{stack_info.env_suffix}/",
    tags=aws_config.tags,
)
iam.RolePolicyAttachment(
    "edx-platform-describe-instances-permission",
    policy_arn=policy_stack.require_output("iam_policies")["describe_instances"],
    role=edx_platform_iam_role.name,
)
iam.RolePolicyAttachment(
    "edx-platform-role-policy",
    policy_arn=edx_platform_policy.arn,
    role=edx_platform_iam_role.name,
)

##################################
#     Network Access Control     #
##################################
group_name = f"edx-platform-mitxonline-{stack_info.env_suffix}"
edx_platform_security_group = ec2.SecurityGroup(
    "edx-platform-security-group",
    name=group_name,
    egress=default_egress_args,
    tags=aws_config.merged_tags({"Name": group_name}),
)

# Create security group for Mitxonline MariaDB database
mitxonline_db_security_group = ec2.SecurityGroup(
    f"mitxonline-db-access-{stack_info.env_suffix}",
    name=f"mitxonline-db-access-{stack_info.env_suffix}",
    description="Access from Mitxonline instances to the associated MariaDB database",
    ingress=[
        ec2.SecurityGroupIngressArgs(
            security_groups=[edx_platform_security_group.id],
            # TODO: Create Vault security group to act as source of allowed
            # traffic. (TMM 2021-05-04)
            cidr_blocks=[mitxonline_vpc["cidr"]],
            protocol="tcp",
            from_port=DEFAULT_MYSQL_PORT,
            to_port=DEFAULT_MYSQL_PORT,
            description="Access to MariaDB from Mitxonline web nodes",
        ),
    ],
    tags=aws_config.tags,
    vpc_id=mitxonline_vpc_id,
)


##########################
#     Database Setup     #
##########################
mitxonline_db_config = OLMariaDBConfig(
    instance_name=f"mitxonline-db-{stack_info.env_suffix}",
    password=mitxonline_config.require("db_password"),
    subnet_group_name=mitxonline_vpc["rds_subnet"],
    security_groups=[mitxonline_db_security_group],
    tags=aws_config.tags,
    db_name="edxapp",
    engine_version="10.5.8",
    **defaults(stack_info)["rds"],
)
mitxonline_db = OLAmazonDB(mitxonline_db_config)

mitxonline_db_vault_backend_config = OLVaultMysqlDatabaseConfig(
    db_name=mitxonline_db_config.db_name,
    mount_point=f"{mitxonline_db_config.engine}-mitxonline",
    db_admin_username=mitxonline_db_config.username,
    db_admin_password=mitxonline_config.require("db_password"),
    db_host=mitxonline_db.db_instance.address,
)
mitxonline_db_vault_backend = OLVaultDatabaseBackend(mitxonline_db_vault_backend_config)

mitxonline_db_consul_node = Node(
    "mitxonline-instance-db-node",
    name="mitxonline-mysql",
    address=mitxonline_db.db_instance.address,
    datacenter=f"mitxonline-{stack_info.env_suffix}",
)

mitxonline_db_consul_service = Service(
    "mitxonline-instance-db-service",
    node=mitxonline_db_consul_node.name,
    name="mitxonline-mariadb",
    port=mitxonline_db_config.port,
    meta={
        "external-node": True,
        "external-probe": True,
    },
    checks=[
        ServiceCheckArgs(
            check_id="mitxonline-db",
            interval="10s",
            name="mitxonline-database",
            timeout="60s",
            status="passing",
            tcp=f"{mitxonline_db.db_instance.address}:{mitxonline_db_config.port}",  # noqa: WPS237,E501
        )
    ],
)


###########################
# Redis Elasticache Setup #
###########################

redis_config = Config("redis")
redis_cluster_security_group = ec2.SecurityGroup(
    f"edxapp-redis-cluster-{env_name}",
    name=f"edxapp-redis-{env_name}",
    description="Grant access to Redis from Open edX",
    ingress=[
        ec2.SecurityGroupIngressArgs(
            from_port=DEFAULT_REDIS_PORT,
            to_port=DEFAULT_REDIS_PORT,
            protocol="tcp",
            security_groups=[edx_platform_security_group.id],
            description="Allow access from edX to Redis for caching and queueing",
        )
    ],
    tags=aws_config.merged_tags({"Name": f"edxapp-redis-{env_name}"}),
    vpc_id=mitxonline_vpc_id,
)

redis_cache_config = OLAmazonRedisConfig(
    encrypt_transit=True,
    auth_token=redis_config.require("auth_token"),
    encrypted=True,
    engine_version="6.x",
    num_instances=3,
    shard_count=1,
    auto_upgrade=True,
    cluster_description="Redis cluster for edX platform tasks and caching",
    cluster_name=f"edxapp-redis-{stack_info.env_suffix}",
    security_groups=[redis_cluster_security_group.id],
    subnet_group=mitxonline_vpc[
        "elasticache_subnet"
    ],  # the name of the subnet group created in the OLVPC component resource
    tags=aws_config.tags,
    **defaults(stack_info)["redis"],
)
