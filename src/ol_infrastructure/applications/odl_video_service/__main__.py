"""The complete state needed to provision OVS running on Docker.

"""
import base64
import json
import textwrap
from functools import partial
from itertools import chain
from pathlib import Path

import pulumi_consul as consul
import pulumi_vault as vault
import yaml
from pulumi import Config, Output, ResourceOptions, StackReference, export
from pulumi_aws import ec2, get_ami, get_caller_identity, iam, s3
from pulumi_consul import Node, Service, ServiceCheckArgs

from bridge.lib.magic_numbers import (
    AWS_RDS_DEFAULT_DATABASE_CAPACITY,
    DEFAULT_HTTPS_PORT,
    DEFAULT_POSTGRES_PORT,
    DEFAULT_REDIS_PORT,
)
from bridge.secrets.sops import read_yaml_secrets
from ol_infrastructure.components.aws.auto_scale_group import (
    BlockDeviceMapping,
    OLAutoScaleGroupConfig,
    OLAutoScaling,
    OLLaunchTemplateConfig,
    OLLoadBalancerConfig,
    OLTargetGroupConfig,
    TagSpecification,
)
from ol_infrastructure.components.aws.cache import OLAmazonCache, OLAmazonRedisConfig
from ol_infrastructure.components.aws.database import OLAmazonDB, OLPostgresDBConfig
from ol_infrastructure.components.services.vault import (
    OLVaultDatabaseBackend,
    OLVaultPostgresDatabaseConfig,
)
from ol_infrastructure.lib.aws.ec2_helper import InstanceTypes, default_egress_args
from ol_infrastructure.lib.consul import get_consul_provider
from ol_infrastructure.lib.ol_types import AWSBase
from ol_infrastructure.lib.pulumi_helper import parse_stack
from ol_infrastructure.lib.stack_defaults import defaults
from ol_infrastructure.lib.vault import setup_vault_provider


# A little helper to make populating the 9000 consul keys needed
# by this application a bit less painful
def consul_key_helper(key_value_mapping):
    keys = []
    for key, val in key_value_mapping.items():  # noqa: WPS110
        keys.append(
            consul.KeysKeyArgs(
                path=key,
                value=val,
            )
        )
    consul.Keys("ovs-server-configuration-data", keys=keys, opts=consul_provider)


# Configuration items and initialziations
if Config("vault_server").get("env_namespace"):
    setup_vault_provider()
ovs_config = Config("ovs")
stack_info = parse_stack()

aws_account = get_caller_identity()

network_stack = StackReference(f"infrastructure.aws.network.{stack_info.name}")
policy_stack = StackReference("infrastructure.aws.policies")
dns_stack = StackReference("infrastructure.aws.dns")
consul_stack = StackReference(f"infrastructure.consul.apps.{stack_info.name}")
vault_stack = StackReference(f"infrastructure.vault.operations.{stack_info.name}")

target_vpc_name = ovs_config.get("target_vpc") or f"{stack_info.env_prefix}_vpc"
target_vpc = network_stack.require_output(target_vpc_name)
target_vpc_id = target_vpc["id"]

# We will take the entire secret structure and load it into Vault as is under the root mount
# further down in the file
secrets = read_yaml_secrets(
    Path(f"odl_video_service/data.{stack_info.env_suffix}.yaml")
)

aws_config = AWSBase(
    tags={
        "OU": "odl-video",
        "Environment": f"applications_{stack_info.env_suffix}",
    }
)
consul_provider = get_consul_provider(stack_info)

env_name = f"{stack_info.env_prefix}-{stack_info.env_suffix}"

# IAM and instance profile
ovs_server_instance_role = iam.Role(
    f"odl-video-service-server-instance-role-{stack_info.env_suffix}",
    assume_role_policy=json.dumps(
        {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Action": "sts:AssumeRole",
                    "Principal": {"Service": "ec2.amazonaws.com"},
                }
            ],
        }
    ),
    path="/ol-infrastructure/odl-video-service-server/role/",
    tags=aws_config.tags,
)

iam.RolePolicyAttachment(
    f"odl-video-service-server-describe-instance-role-policy-{env_name}",
    policy_arn=policy_stack.require_output("iam_policies")["describe_instances"],
    role=ovs_server_instance_role.name,
)
iam.RolePolicyAttachment(
    f"odl-video-service-server-route53-role-policy-{env_name}",
    policy_arn=policy_stack.require_output("iam_policies")["route53_odl_zone_records"],
    role=ovs_server_instance_role.name,
)

ovs_server_instance_profile = iam.InstanceProfile(
    f"odl-video-service-server-instance-profile-{env_name}",
    role=ovs_server_instance_role.name,
    path="/ol-infrastructure/odl-video-service-server/profile/",
)

# Network Access Control

# Create various security groups
ovs_server_security_group = ec2.SecurityGroup(
    f"odl-video-service-server-security-group-{env_name}",
    name=f"odl-video-service-server-{target_vpc_name}-{env_name}",
    description="Access control for odl-video-service servers",
    ingress=[
        ec2.SecurityGroupIngressArgs(
            protocol="tcp",
            from_port=DEFAULT_HTTPS_PORT,
            to_port=DEFAULT_HTTPS_PORT,
            cidr_blocks=["0.0.0.0/0"],
            description=f"Allow traffic to the odl-video-service server on port {DEFAULT_HTTPS_PORT}",
        ),
    ],
    egress=default_egress_args,
    vpc_id=target_vpc_id,
    tags=aws_config.tags,
)

ovs_database_security_group = ec2.SecurityGroup(
    f"odl-video-service-database-security-group-{env_name}",
    name=f"odl-video-service-database-{target_vpc_name}-{env_name}",
    description="Access control for the odl-video-service database",
    ingress=[
        ec2.SecurityGroupIngressArgs(
            security_groups=[
                ovs_server_security_group.id,
                consul_stack.require_output("security_groups")["consul_server"],
                vault_stack.require_output("vault_server")["security_group"],
            ],
            cidr_blocks=[target_vpc["cidr"]],
            protocol="tcp",
            from_port=DEFAULT_POSTGRES_PORT,
            to_port=DEFAULT_POSTGRES_PORT,
            description=f"Access to Postgres from odl-video-service nodes on {DEFAULT_POSTGRES_PORT}",
        ),
    ],
    vpc_id=target_vpc_id,
    tags=aws_config.tags,
)

ovs_redis_security_group = ec2.SecurityGroup(
    f"odl-video-service-redis-security-group-{env_name}",
    name=f"odl-video-service-redis-{target_vpc_name}-{env_name}",
    description="Access control for the odl-video-service redis queue",
    ingress=[
        ec2.SecurityGroupIngressArgs(
            security_groups=[
                ovs_server_security_group.id,
            ],
            # cidr_blocks=[target_vpc["cidr"]], # noqa: E800
            protocol="tcp",
            from_port=DEFAULT_REDIS_PORT,
            to_port=DEFAULT_REDIS_PORT,
            description=f"Access to Redis from odl-video-service nodes on {DEFAULT_REDIS_PORT}",
        ),
    ],
    egress=default_egress_args,
    vpc_id=target_vpc_id,
    tags=aws_config.tags,
)

# Database
rds_defaults = defaults(stack_info)["rds"]

rds_password = ovs_config.require("rds_password")

ovs_db_config = OLPostgresDBConfig(
    instance_name=f"odl-video-service-{stack_info.env_suffix}",
    password=rds_password,
    storage=ovs_config.get("db_capacity") or str(AWS_RDS_DEFAULT_DATABASE_CAPACITY),
    subnet_group_name=target_vpc["rds_subnet"],
    security_groups=[ovs_database_security_group],
    parameter_overrides=[],
    db_name="odlvideo",
    engine_version="12.8",
    tags=aws_config.tags,
    **rds_defaults,
)
ovs_db = OLAmazonDB(ovs_db_config)

db_address = ovs_db.db_instance.address
db_port = ovs_db.db_instance.port
db_name = ovs_db.db_instance.name

ovs_db_vault_backend_config = OLVaultPostgresDatabaseConfig(
    db_name=ovs_db_config.db_name,
    mount_point=f"{ovs_db_config.engine}-odl-video-service",
    db_admin_username=ovs_db_config.username,
    db_admin_password=rds_password,
    db_host=db_address,
)
ovs_db_vault_backend = OLVaultDatabaseBackend(ovs_db_vault_backend_config)

ovs_db_consul_node = Node(
    f"odl-video-service-{stack_info.env_suffix}-db-node",
    name="ovs-postgres-db",
    address=db_address,
    opts=consul_provider,
)

ovs_db_consul_service = Service(
    f"odl-video-service-{stack_info.env_suffix}-db-service",
    node=ovs_db_consul_node.name,
    name="ovs-postgres",
    port=db_port,
    meta={
        "external-node": True,
        "external-probe": True,
    },
    checks=[
        ServiceCheckArgs(
            check_id="ovs-instance-db",
            interval="10s",
            name="ovs-instance-db",
            timeout="60s",
            status="passing",
            tcp=Output.all(
                address=db_address,
                port=db_port,
            ).apply(lambda db: "{address}:{port}".format(**db)),
        )
    ],
    opts=consul_provider,
)

redis_auth_token = secrets["redis"]["auth_token"]
redis_config = Config("redis")

ovs_server_redis_config = OLAmazonRedisConfig(
    encrypt_transit=True,
    auth_token=redis_auth_token,
    engine_version="6.2",
    num_instances=3,
    shard_count=1,
    auto_upgrade=True,
    cluster_mode_enabled=False,
    cluster_description="Redis cluster for ODL Video Service.",
    cluster_name=f"odl-video-service-redis-{stack_info.env_suffix}",
    security_groups=[ovs_redis_security_group.id],
    subnet_group=target_vpc["elasticache_subnet"],
    tags=aws_config.tags,
    **defaults(stack_info)["redis"],
)
ovs_server_redis_cluster = OLAmazonCache(ovs_server_redis_config)

# Provision EC2 resources
instance_type_name = (
    ovs_config.get("instance_type") or InstanceTypes.burstable_medium.name
)
instance_type = InstanceTypes[instance_type_name].value

subnets = target_vpc["subnet_ids"]
subnet_id = subnets.apply(chain)

grafana_credentials = read_yaml_secrets(
    Path(f"vector/grafana.{stack_info.env_suffix}.yaml")
)
consul_datacenter = consul_stack.require_output("datacenter")

instance_tags = aws_config.merged_tags(
    {"Name": f"odl-video-service-{stack_info.env_suffix}"}
)

ovs_server_ami = get_ami(
    filters=[
        {
            "name": "tag:Name",
            "values": ["odl_video_service-server"],
        },
        {
            "name": "virtualization-type",
            "values": ["hvm"],
        },
    ],
    most_recent=True,
    owners=[str(aws_account.id)],
)

block_device_mappings = [BlockDeviceMapping()]

ovs_lb_config = OLLoadBalancerConfig(
    subnets=subnets,
    security_groups=[ovs_server_security_group],
    tags=instance_tags,
)

ovs_tg_config = OLTargetGroupConfig(
    vpc_id=target_vpc["id"],
    target_group_healthcheck=False,
    health_check_interval=60,
    health_check_matcher="200",
    health_check_path="/ping",
    tags=instance_tags,
)

ovs_lt_config = OLLaunchTemplateConfig(
    block_device_mappings=block_device_mappings,
    image_id=ovs_server_ami.id,
    instance_type=instance_type,
    instance_profile_arn=ovs_server_instance_profile.arn,
    security_groups=[
        target_vpc["security_groups"]["default"],
        target_vpc["security_groups"]["web"],
        ovs_database_security_group,
        ovs_redis_security_group,
    ],
    tags=instance_tags,
    tag_specifications=[
        TagSpecification(
            resource_type="instance",
            tags=instance_tags,
        ),
        TagSpecification(
            resource_type="volume",
            tags=instance_tags,
        ),
    ],
    user_data=consul_datacenter.apply(
        lambda consul_dc: base64.b64encode(
            "#cloud-config\n{}".format(
                yaml.dump(
                    {
                        "write_files": [
                            {
                                "path": "/etc/consul.d/02-autojoin.json",
                                "content": json.dumps(
                                    {
                                        "retry_join": [
                                            "provider=aws tag_key=consul_env "
                                            f"tag_value={consul_dc}"
                                        ],
                                        "datacenter": consul_dc,
                                    }
                                ),
                                "owner": "consul:consul",
                            },
                            {
                                "path": "/etc/default/vector",
                                "content": textwrap.dedent(
                                    f"""\
                            ENVIRONMENT={consul_dc}
                            VECTOR_CONFIG_DIR=/etc/vector/
                            AWS_REGION={aws_config.region}
                            GRAFANA_CLOUD_API_KEY={grafana_credentials['api_key']}
                            GRAFANA_CLOUD_PROMETHEUS_API_USER={grafana_credentials['prometheus_user_id']}
                            GRAFANA_CLOUD_LOKI_API_USER={grafana_credentials['loki_user_id']}
                            """
                                ),  # noqa: WPS355
                                "owner": "root:root",
                            },
                        ],
                    }
                ),
            ).encode("utf8")
        ).decode("utf8")
    ),
)

ovs_autoscale_sizes = ovs_config.get_object("auto_scale") or {
    "desired": 2,
    "min": 1,
    "max": 3,
}
ovs_asg_config = OLAutoScaleGroupConfig(
    asg_name=f"odl-video-service-{stack_info.env_suffix}",
    aws_config=aws_config,
    desired_size=ovs_autoscale_sizes["desired"],
    min_size=ovs_autoscale_sizes["min"],
    max_size=ovs_autoscale_sizes["max"],
    vpc_zone_identifiers=target_vpc["subnet_ids"],
    tags=instance_tags,
)

autoscale_setup = OLAutoScaling(
    asg_config=ovs_asg_config,
    lt_config=ovs_lt_config,
    tg_config=ovs_tg_config,
    lb_config=ovs_lb_config,
)

# Vault policy definition
ovs_server_vault_policy = vault.Policy(
    "ovs-server-vault-policy",
    name="odl-video-service-server",
    policy=Path(__file__)
    .parent.joinpath("odl_video_service_server_policy.hcl")
    .read_text(),
)

vault.aws.AuthBackendRole(
    "odl-video-service-server-ec2-vault-auth",
    backend="aws",
    auth_type="iam",
    role="ovs-server",
    inferred_entity_type="ec2_instance",
    inferred_aws_region=aws_config.region,
    bound_iam_instance_profile_arns=[ovs_server_instance_profile.arn],
    bound_ami_ids=[ovs_server_ami.id],
    bound_account_ids=[aws_account.account_id],
    bound_vpc_ids=[target_vpc_id],
    token_policies=[ovs_server_vault_policy.name],
)

# Vault KV2 mount definition
ovs_server_vault_mount = vault.Mount(
    "ovs-server-configuration-secrets-mount",
    path="secret-odl-video-service",
    type="kv-v2",
    options={"version": 2},
    description="Storage of configuration credentials and secrets used by odl-video-service",
    opts=ResourceOptions(delete_before_replace=True),
)

secret_paths = [
    "cloudfront",
    "dropbox",
    "google",
    "mailgun",
    "misc",
    "openedx",
    "redis",
    "sentry",
    "youtube",
]
for secret_path in secret_paths:
    short_lived_partial = partial(
        "{1}/{0}".format,
        secret_path,
    )
    vault.generic.Secret(
        f"ovs-server-configuration-{secret_path}-secrets",
        path=ovs_server_vault_mount.path.apply(short_lived_partial),
        data_json=json.dumps(secrets[secret_path]),
    )


consul_keys = {
    "ovs/database_endpoint": db_address,
    "ovs/redis_cluster_address": ovs_server_redis_cluster.address,
    "ovs/log_level": ovs_config.get("log_level"),
    "ovs/edx_base_url": ovs_config.get("edx_base_url"),
    "ovs/domain": ovs_config.get("domain"),
    "ovs/environment": stack_info.env_suffix,
    "ovs/redis_max_connections": redis_config.get("max_connections")
    or 65000,  # noqa: WPS432
    "ovs/use_shibboleth": True,
    "ovs/s3_bucket_name": ovs_config.get("s3_bucket_name"),
    "ovs/s3_subtitle_bucket_name": ovs_config.get("s3_subtitle_bucket_name"),
    "ovs/s3_thumbnail_bucket_name": ovs_config.get("s3_thumbnail_bucket_name"),
    "ovs/s3_transcode_bucket_name": ovs_config.get("s3_transcode_bucket_name"),
    "ovs/s3_watch_bucket_name": ovs_config.get("s3_watch_bucket_name"),
}
consul_key_helper(consul_keys)

# Create a S3 bucket for hosting the static assets
static_assets_bucket_name = f"ovs-static-assets-{stack_info.env_suffix}"
static_assets_bucket_arn = f"arn:aws:s3:::{static_assets_bucket_name}"

static_assets_bucket = s3.Bucket(
    "ovs-server-static-assets-bucket",
    bucket=static_assets_bucket_name,
    tags=aws_config.tags,
    acl="public-read",
    policy=json.dumps(
        {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Sid": "PublicRead",
                    "Effect": "Allow",
                    "Principal": "*",
                    "Action": ["s3:GetObject"],
                    "Resource": [f"{static_assets_bucket_arn}/*"],
                }
            ],
        }
    ),
    cors_rules=[{"allowedMethods": ["GET", "HEAD"], "allowedOrigins": ["*"]}],
)


# TODO MD 20221011 revisit this, probably need to export more things
export(
    "odl_video_service",
    {
        "rds_host": db_address,
        "redis_cluster": ovs_server_redis_cluster.address,
    },
)
