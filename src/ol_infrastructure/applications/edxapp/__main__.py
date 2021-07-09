# TODO: Manage database object creation
import json
from pathlib import Path
from string import Template

import pulumi_vault as vault
from pulumi import Config, StackReference, export
from pulumi_aws import ec2, get_caller_identity, iam, s3
from pulumi_consul import Node, Service, ServiceCheckArgs

from bridge.lib.magic_numbers import DEFAULT_MYSQL_PORT, DEFAULT_REDIS_PORT
from ol_infrastructure.components.aws.cache import OLAmazonCache, OLAmazonRedisConfig
from ol_infrastructure.components.aws.database import OLAmazonDB, OLMariaDBConfig
from ol_infrastructure.components.services.vault import (
    OLVaultDatabaseBackend,
    OLVaultMongoDatabaseConfig,
    OLVaultMysqlDatabaseConfig,
)
from ol_infrastructure.lib.aws.ec2_helper import default_egress_args
from ol_infrastructure.lib.aws.iam_helper import IAM_POLICY_VERSION, lint_iam_policy
from ol_infrastructure.lib.ol_types import AWSBase
from ol_infrastructure.lib.pulumi_helper import parse_stack
from ol_infrastructure.lib.stack_defaults import defaults
from ol_infrastructure.lib.vault import mysql_role_statements

edxapp_config = Config("edxapp")
stack_info = parse_stack()
network_stack = StackReference(f"infrastructure.aws.network.{stack_info.name}")
policy_stack = StackReference("infrastructure.aws.policies")
dns_stack = StackReference("infrastructure.aws.dns")
consul_stack = StackReference(
    f"infrastructure.consul.{stack_info.env_prefix}.{stack_info.name}"
)
edxapp_vpc = network_stack.require_output(f"{stack_info.env_prefix}_vpc")
operations_vpc = network_stack.require_output("operations_vpc")
env_name = f"{stack_info.env_prefix}-{stack_info.env_suffix}"
aws_config = AWSBase(
    tags={"OU": edxapp_config.require("business_unit"), "Environment": env_name}
)

aws_account = get_caller_identity()
edxapp_vpc_id = edxapp_vpc["id"]
edxapp_web_ami = ec2.get_ami(
    filters=[
        ec2.GetAmiFilterArgs(name="name", values=["edxapp-web-*"]),
        ec2.GetAmiFilterArgs(name="virtualization-type", values=["hvm"]),
        ec2.GetAmiFilterArgs(name="root-device-type", values=["ebs"]),
    ],
    most_recent=True,
    owners=[aws_account.account_id],
)

edxapp_worker_ami = ec2.get_ami(
    filters=[
        ec2.GetAmiFilterArgs(name="name", values=["edxapp-worker-*"]),
        ec2.GetAmiFilterArgs(name="virtualization-type", values=["hvm"]),
        ec2.GetAmiFilterArgs(name="root-device-type", values=["ebs"]),
    ],
    most_recent=True,
    owners=[aws_account.account_id],
)

##############
# S3 Buckets #
##############

storage_bucket_name = f"{stack_info.env_prefix}-edxapp-storage-{stack_info.env_suffix}"
edxapp_storage_bucket = s3.Bucket(
    "edxapp-storage-bucket",
    bucket=storage_bucket_name,
    versioning=s3.BucketVersioningArgs(enabled=True),
    tags=aws_config.tags,
)

course_bucket_name = f"{stack_info.env_prefix}-edxapp-courses-{stack_info.env_suffix}"
edxapp_storage_bucket = s3.Bucket(
    "edxapp-courses-bucket",
    bucket=course_bucket_name,
    versioning=s3.BucketVersioningArgs(enabled=False),
    tags=aws_config.tags,
)

grades_bucket_name = f"{stack_info.env_prefix}-edxapp-grades-{stack_info.env_suffix}"
edxapp_storage_bucket = s3.Bucket(
    "edxapp-grades-bucket",
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

edxapp_policy_document = {
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
                "s3:GetObject*",
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
                f"arn:aws:s3:::{course_bucket_name}",
                f"arn:aws:s3:::{course_bucket_name}/*",
            ],
        },
    ],
}

edxapp_policy = iam.Policy(
    "edxapp-policy",
    name_prefix="edxapp-policy-",
    path=f"/ol-applications/edxapp/{stack_info.env_prefix}/{stack_info.env_suffix}/",
    policy=lint_iam_policy(
        edxapp_policy_document,
        stringify=True,
        parliament_config=parliament_config,
    ),
    description="AWS access permissions for edX application instances",
)
edxapp_iam_role = iam.Role(
    "edxapp-instance-role",
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
    name_prefix=f"{stack_info.env_prefix}-edxapp-role-{stack_info.env_suffix}-",
    path=f"/ol-applications/edxapp/{stack_info.env_prefix}/{stack_info.env_suffix}/",
    tags=aws_config.tags,
)
iam.RolePolicyAttachment(
    "edxapp-describe-instances-permission",
    policy_arn=policy_stack.require_output("iam_policies")["describe_instances"],
    role=edxapp_iam_role.name,
)
iam.RolePolicyAttachment(
    "edxapp-role-policy",
    policy_arn=edxapp_policy.arn,
    role=edxapp_iam_role.name,
)
edxapp_instance_profile = iam.InstanceProfile(
    f"edxapp-instance-profile-{stack_info.env_suffix}",
    name_prefix=f"{stack_info.env_prefix}-edxapp-role-{stack_info.env_suffix}-",
    role=edxapp_iam_role.name,
    path=f"/ol-applications/edxapp/{stack_info.env_prefix}/",
)
##################################
#     Network Access Control     #
##################################
group_name = f"edxapp-{env_name}"
edxapp_security_group = ec2.SecurityGroup(
    "edxapp-security-group",
    name_prefix=f"{group_name}-",
    ingress=[],
    egress=default_egress_args,
    tags=aws_config.merged_tags({"Name": group_name}),
    vpc_id=edxapp_vpc_id,
)

# Create security group for edxapp MariaDB database
edxapp_db_security_group = ec2.SecurityGroup(
    f"edxapp-db-access-{stack_info.env_suffix}",
    name_prefix=f"edxapp-db-access-{env_name}-",
    description="Access from Edxapp instances to the associated MariaDB database",
    ingress=[
        ec2.SecurityGroupIngressArgs(
            security_groups=[edxapp_security_group.id],
            # TODO: Create Vault security group to act as source of allowed
            # traffic. (TMM 2021-05-04)
            cidr_blocks=[
                edxapp_vpc["cidr"],
                operations_vpc["cidr"],
            ],
            protocol="tcp",
            from_port=DEFAULT_MYSQL_PORT,
            to_port=DEFAULT_MYSQL_PORT,
            description="Access to MariaDB from Edxapp web nodes",
        ),
    ],
    tags=aws_config.tags,
    vpc_id=edxapp_vpc_id,
)


##########################
#     Database Setup     #
##########################
edxapp_db_config = OLMariaDBConfig(
    instance_name=f"edxapp-db-{env_name}",
    password=edxapp_config.require("db_password"),
    subnet_group_name=edxapp_vpc["rds_subnet"],
    security_groups=[edxapp_db_security_group],
    tags=aws_config.tags,
    db_name="edxapp",
    engine_version="10.5.8",
    **defaults(stack_info)["rds"],
)
edxapp_db = OLAmazonDB(edxapp_db_config)

edxapp_mysql_role_statements = mysql_role_statements.copy()
edxapp_mysql_role_statements.pop("app")
edxapp_mysql_role_statements["edxapp"] = {
    "create": Template(
        "CREATE USER '{{name}}'@'%' IDENTIFIED BY '{{password}}';"
        "GRANT SELECT, INSERT, UPDATE, DELETE, CREATE, INDEX, DROP, ALTER, REFERENCES, "
        "CREATE TEMPORARY TABLES, LOCK TABLES ON edxapp.* TO '{{name}}'@'%';"
    ),
    "revoke": Template("DROP USER '{{name}}';"),
}
edxapp_mysql_role_statements["edxapp-csmh"] = {
    "create": Template(
        "CREATE USER '{{name}}'@'%' IDENTIFIED BY '{{password}}';"
        "GRANT SELECT, INSERT, UPDATE, DELETE, CREATE, INDEX, DROP, ALTER, REFERENCES, "
        "CREATE TEMPORARY TABLES, LOCK TABLES ON edxapp_csmh.* TO '{{name}}'@'%';"
    ),
    "revoke": Template("DROP USER '{{name}}';"),
}
edxapp_mysql_role_statements["xqueue"] = {
    "create": Template(
        "CREATE USER '{{name}}'@'%' IDENTIFIED BY '{{password}}';"
        "GRANT SELECT, INSERT, UPDATE, DELETE, CREATE, INDEX, DROP, ALTER, REFERENCES, "
        "CREATE TEMPORARY TABLES, LOCK TABLES ON xqueue.* TO '{{name}}'@'%';"
    ),
    "revoke": Template("DROP USER '{{name}}';"),
}

edxapp_db_vault_backend_config = OLVaultMysqlDatabaseConfig(
    db_name=edxapp_db_config.db_name,
    mount_point=f"{edxapp_db_config.engine}-{stack_info.env_prefix}",
    db_admin_username=edxapp_db_config.username,
    db_admin_password=edxapp_config.require("db_password"),
    db_host=edxapp_db.db_instance.address,
    role_statements=edxapp_mysql_role_statements,
)
edxapp_db_vault_backend = OLVaultDatabaseBackend(edxapp_db_vault_backend_config)

edxapp_db_consul_node = Node(
    "edxapp-instance-db-node",
    name="edxapp-mysql",
    address=edxapp_db.db_instance.address,
    datacenter=env_name,
)

edxapp_db_consul_service = Service(
    "edxapp-instance-db-service",
    node=edxapp_db_consul_node.name,
    name="edxapp-db",
    port=edxapp_db_config.port,
    meta={
        "external-node": True,
        "external-probe": True,
    },
    checks=[
        ServiceCheckArgs(
            check_id="edxapp-db",
            interval="10s",
            name="edxapp-db",
            timeout="60s",
            status="passing",
            tcp=f"{edxapp_db.db_instance.address}:{edxapp_db_config.port}",  # noqa: WPS237,E501
        )
    ],
)

#######################
# MongoDB Vault Setup #
#######################
edxapp_mongo_vault_config = OLVaultMongoDatabaseConfig(
    db_name="edxapp",
    mount_point=f"mongodb-{stack_info.env_prefix}",
    db_admin_username="admin",
    db_admin_password=edxapp_config.require("mongo_admin_password"),
    db_host=f"mongodb-master.service.{env_name}.consul",
)
edxapp_mongo_vault_backend = OLVaultDatabaseBackend(edxapp_mongo_vault_config)

###########################
# Redis Elasticache Setup #
###########################

redis_config = Config("redis")
redis_cluster_security_group = ec2.SecurityGroup(
    f"edxapp-redis-cluster-{env_name}",
    name_prefix=f"edxapp-redis-{env_name}-",
    description="Grant access to Redis from Open edX",
    ingress=[
        ec2.SecurityGroupIngressArgs(
            from_port=DEFAULT_REDIS_PORT,
            to_port=DEFAULT_REDIS_PORT,
            protocol="tcp",
            security_groups=[edxapp_security_group.id],
            description="Allow access from edX to Redis for caching and queueing",
        )
    ],
    tags=aws_config.merged_tags({"Name": f"edxapp-redis-{env_name}"}),
    vpc_id=edxapp_vpc_id,
)

redis_cache_config = OLAmazonRedisConfig(
    encrypt_transit=True,
    auth_token=redis_config.require("auth_token"),
    cluster_mode_enabled=False,
    encrypted=True,
    engine_version="6.x",
    num_instances=3,
    shard_count=1,
    auto_upgrade=True,
    cluster_description="Redis cluster for edX platform tasks and caching",
    cluster_name=f"edxapp-redis-{env_name}",
    security_groups=[redis_cluster_security_group.id],
    subnet_group=edxapp_vpc[
        "elasticache_subnet"
    ],  # the name of the subnet group created in the OLVPC component resource
    tags=aws_config.tags,
    **defaults(stack_info)["redis"],
)
edxapp_redis_cache = OLAmazonCache(redis_cache_config)
edxapp_redis_consul_node = Node(
    "edxapp-redis-cache-node",
    name="edxapp-redis",
    address=edxapp_redis_cache.address,
    datacenter=env_name,
)

edxapp_redis_consul_service = Service(
    "edxapp-redis-consul-service",
    node=edxapp_redis_consul_node.name,
    name="edxapp-redis",
    port=redis_cache_config.port,
    meta={
        "external-node": True,
        "external-probe": True,
    },
    checks=[
        ServiceCheckArgs(
            check_id="edxapp-redis",
            interval="10s",
            name="edxapp-redis",
            timeout="60s",
            status="passing",
            tcp=f"{edxapp_redis_cache.address}:{edxapp_redis_cache.cache_cluster.port}",  # noqa: WPS237,E501
        )
    ],
)

######################
# Secrets Management #
######################
edxapp_vault_mount = vault.Mount(
    "edxapp-vault-generic-secrets-mount",
    path=f"secret-{stack_info.env_prefix}",
    description="Static secrets storage for MITx Online applications and services",
    type="kv",
)
edxapp_secrets = vault.generic.Secret(
    "edxapp-static-secrets",
    path=edxapp_vault_mount.path.apply("{}/edxapp".format),
    data_json=edxapp_config.require_secret_object("edxapp_secrets").apply(json.dumps),
)
forum_secrets = vault.generic.Secret(
    "edx-forum-static-secrets",
    path=edxapp_vault_mount.path.apply("{}/edx-forum".format),
    data_json=edxapp_config.require_secret_object("edx_forum_secrets").apply(
        json.dumps
    ),
)

# Vault policy definition
edxapp_vault_policy = vault.Policy(
    "edxapp-vault-policy",
    name="edxapp",
    policy=Path(__file__).parent.joinpath("edxapp_policy.hcl").read_text(),
)
# Register edX Platform AMI for Vault AWS auth
vault.aws.AuthBackendRole(
    "edxapp-web-ami-ec2-vault-auth",
    backend="aws",
    auth_type="iam",
    role="edxapp-web",
    inferred_entity_type="ec2_instance",
    inferred_aws_region="us-east-1",
    bound_iam_instance_profile_arns=[edxapp_instance_profile.arn],
    bound_ami_ids=[edxapp_web_ami.id],
    bound_account_ids=[aws_account.account_id],
    bound_vpc_ids=[edxapp_vpc_id],
    token_policies=[edxapp_vault_policy.name],
)

vault.aws.AuthBackendRole(
    "edxapp-worker-ami-ec2-vault-auth",
    backend="aws",
    auth_type="iam",
    role="edxapp-worker",
    inferred_entity_type="ec2_instance",
    inferred_aws_region="us-east-1",
    bound_iam_instance_profile_arns=[edxapp_instance_profile.arn],
    bound_ami_ids=[edxapp_worker_ami.id],
    bound_account_ids=[aws_account.account_id],
    bound_vpc_ids=[edxapp_vpc_id],
    token_policies=[edxapp_vault_policy.name],
)


export(
    f"{stack_info.env_prefix}_edxapp",
    {
        "mariadb": edxapp_db.db_instance.address,
        "redis": edxapp_redis_cache.address,
    },
)
