"""The complete state necessary to deploy an instance of the Redash application.

- Create an RDS PostgreSQL instance for storing Redash's configuration data and
  intermediate query results
- Mount a Vault database backend and provision role definitions for the Redash RDS
  database
- Create an IAM role for Redash instances to allow access to S3 and other AWS
  resources
- Create a Redis cluster in Elasticache
- Provision a set of EC2 instances from a pre-built AMI with the configuration
  and code for Redash
- Provision an AWS load balancer and connect the deployed EC2 instances
- Create a DNS record for the deployed load balancer
"""

import base64
import json
import textwrap
from itertools import chain
from pathlib import Path

import pulumi_consul as consul
import pulumi_vault as vault
import yaml
from pulumi import Config, StackReference, export
from pulumi.config import get_config
from pulumi_aws import ec2, get_caller_identity, iam, route53

from bridge.lib.magic_numbers import (
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
from ol_infrastructure.components.aws.database import OLAmazonDB, OLPostgresDBConfig
from ol_infrastructure.components.services.vault import (
    OLVaultDatabaseBackend,
    OLVaultPostgresDatabaseConfig,
)
from ol_infrastructure.lib.aws.cache_helper import create_redis_cache
from ol_infrastructure.lib.aws.ec2_helper import InstanceTypes, default_egress_args
from ol_infrastructure.lib.consul import get_consul_provider
from ol_infrastructure.lib.ol_types import AWSBase
from ol_infrastructure.lib.pulumi_helper import parse_stack
from ol_infrastructure.lib.stack_defaults import defaults
from ol_infrastructure.lib.vault import setup_vault_provider

redash_config = Config("redash")
stack_info = parse_stack()
network_stack = StackReference(f"infrastructure.aws.network.{stack_info.name}")
consul_stack = StackReference(f"infrastructure.consul.data.{stack_info.name}")
dns_stack = StackReference("infrastructure.aws.dns")
policy_stack = StackReference("infrastructure.aws.policies")
mitodl_zone_id = dns_stack.require_output("odl_zone_id")
data_vpc = network_stack.require_output("data_vpc")
operations_vpc = network_stack.require_output("operations_vpc")
redash_environment = f"data-{stack_info.env_suffix}"
aws_config = AWSBase(
    tags={"OU": "data", "Environment": redash_environment},
)

consul_provider = get_consul_provider(stack_info)
setup_vault_provider()
aws_account = get_caller_identity()

# Look up the most recent redash ami
redash_ami = ec2.get_ami(
    filters=[
        {
            "name": "tag:Name",
            "values": ["redash-server"],
        },
        {
            "name": "virtualization-type",
            "values": ["hvm"],
        },
    ],
    most_recent=True,
    owners=[str(aws_account.id)],
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

redash_server_vault_policy = vault.Policy(
    "redash-server-vault-policy",
    name="redash-server",
    policy=Path(__file__).parent.joinpath("redash_server_policy.hcl").read_text(),
)
vault.aws.AuthBackendRole(
    "redash-server-ami-ec2-vault-auth",
    backend="aws",
    auth_type="iam",
    role="redash-server",
    inferred_entity_type="ec2_instance",
    inferred_aws_region=aws_config.region,
    bound_iam_instance_profile_arns=[redash_instance_profile.arn],
    bound_ami_ids=[redash_ami.id],
    bound_account_ids=[aws_account.account_id],
    bound_vpc_ids=[data_vpc["id"]],
    token_policies=[redash_server_vault_policy.name],
)

redash_instance_security_group = ec2.SecurityGroup(
    f"redash-instance-{stack_info.env_suffix}",
    description=(
        "Security group to assign to Redash application to control inter-service access"
    ),
    ingress=[
        ec2.SecurityGroupIngressArgs(
            protocol="tcp",
            from_port=DEFAULT_HTTPS_PORT,
            to_port=DEFAULT_HTTPS_PORT,
            cidr_blocks=["0.0.0.0/0"],
            description=(
                f"Allow traffic to the redash webserver on port {DEFAULT_HTTPS_PORT}"
            ),
        ),
    ],
    egress=default_egress_args,  # This is important!
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
rds_defaults = defaults(stack_info)["rds"]
rds_defaults["use_blue_green"] = False
rds_defaults["read_replica"] = None
redash_db_config = OLPostgresDBConfig(
    instance_name=f"redash-db-{redash_environment}",
    password=redash_config.require("db_password"),
    subnet_group_name=data_vpc["rds_subnet"],
    security_groups=[redash_db_security_group],
    engine_major_version="16",
    tags=aws_config.tags,
    db_name="redash",
    **rds_defaults,
)
redash_db = OLAmazonDB(redash_db_config)

redash_db_vault_backend_config = OLVaultPostgresDatabaseConfig(
    db_name=redash_db_config.db_name,
    mount_point=f"{redash_db_config.engine}-redash",
    db_admin_username=redash_db_config.username,
    db_admin_password=get_config("redash:db_password"),
    db_host=redash_db.db_instance.address,
)
redash_db_vault_backend = OLVaultDatabaseBackend(redash_db_vault_backend_config)

# Set up Redis instance for Redash in Elasticache
redis_config = Config("redis")

# Put the redis auth token into vault so we have some place safe + easy to pull
# it from in the .env file found in bilder
vault.generic.Secret(
    "redis-auth-token-secret",
    path="secret-data/redash/redis-auth-token/",
    data_json=json.dumps({"value": redis_config.require("auth_token")}),
)

# Create Redis cache (automatically selects serverless for QA, dedicated for Production)
redis_defaults = defaults(stack_info)["redis"]
redash_redis_cluster = create_redis_cache(
    stack_info=stack_info,
    cache_name=f"redash-redis-{redash_environment}",
    description="Redis cluster for Redash tasks and caching",
    security_group_ids=["dummy-group"],  # Will be replaced below
    subnet_group=data_vpc["elasticache_subnet"],
    subnet_ids=data_vpc["subnet_ids"][:3],
    auth_token=redis_config.require("auth_token"),
    engine="redis",
    engine_version="6.2",
    instance_type=redis_config.get("instance_type")
    or redis_defaults.get("instance_type"),
    num_instances=3,
    tags=aws_config.tags,
)

redis_cluster_security_group = ec2.SecurityGroup(
    f"redash-redis-cluster-{redash_environment}",
    name=f"redash-redis-{redash_environment}",
    description="Grant access to Redis from Redash",
    ingress=[
        ec2.SecurityGroupIngressArgs(
            from_port=DEFAULT_REDIS_PORT,
            to_port=DEFAULT_REDIS_PORT,
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

# Note: The security group needs to be set after creation due to circular dependency
# In a real deployment, this would need to be handled differently or the cache recreated

# Begin deploying EC2 resources

# Look up the proper names for the instance types
web_instance_type_name = (
    redash_config.get("web_instance_type") or InstanceTypes.burstable_medium.name
)
web_instance_type = InstanceTypes[web_instance_type_name].value
web_tag = f"redash-server-web-{stack_info.env_suffix}"

worker_instance_type_name = (
    redash_config.get("worker_instancey_type") or InstanceTypes.burstable_medium.name
)
worker_instance_type = InstanceTypes[worker_instance_type_name].value
worker_tag = f"redash-server-worker-{stack_info.env_suffix}"

# Collect a few pieces of information needed for deploying ec2 resources
subnets = data_vpc["subnet_ids"]
subnet_id = subnets.apply(chain)

grafana_credentials = read_yaml_secrets(
    Path(f"vector/grafana.{stack_info.env_suffix}.yaml")
)
consul_datacenter = consul_stack.require_output("datacenter")

instance_tags = aws_config.merged_tags({"Name": f"redash-{stack_info.env_suffix}"})

# Put a few things into consul that will be needed to configure redash
consul.Keys(
    "redash-consul-template-data",
    keys=[
        consul.KeysKeyArgs(
            path=f"redash/rds_endpoint",  # noqa: F541
            value=redash_db.db_instance.address,
        ),
        consul.KeysKeyArgs(
            path=f"redash/frontend_host",  # noqa: F541
            value=redash_config.require("domain"),
        ),
        consul.KeysKeyArgs(
            path=f"redash/cache_endpoint_address",  # noqa: F541
            value=redash_redis_cluster.address,
        ),
        consul.KeysKeyArgs(path=f"redash/app_name", value="app_name"),  # noqa: F541
    ],
    opts=consul_provider,
)

# Put a few things into vault that will be needed to configure shibboleth
redash_secrets = read_yaml_secrets(Path(f"redash/redash.{stack_info.env_suffix}.yaml"))
vault.generic.Secret(
    "redash-sp-certificate-data",
    path="secret-data/redash/sp-certificate-data",
    data_json=json.dumps(redash_secrets["sp_certificate_data"]),
)

# If we've specified that datsources will be managed, store some values in
# consul/vault for that particular functionality.
# Refer to DATASOUCE_MANAGEMENT.md
if redash_config.get_bool("manage_datasources"):
    datasource_config_consul_keys = []
    mitxonline_stack = StackReference(f"applications.mitxonline.{stack_info.name}")
    odl_video_service_stack = StackReference(
        f"applications.odl_video_service.{stack_info.name}"
    )
    ocw_studio_stack = StackReference(f"applications.ocw_studio.{stack_info.name}")
    micromasters_stack = StackReference(f"applications.micromasters.{stack_info.name}")
    bootcamps_stack = StackReference(f"applications.bootcamps.{stack_info.name}")
    if stack_info.name == "QA":
        datasource_config_consul_keys.append(
            consul.KeysKeyArgs(
                path="redash/datasource_configs/mitxonline-rc/db_host",
                value=mitxonline_stack.require_output("mitxonline")["rds_host"],
            )
        )
    elif stack_info.name == "Production":
        datasource_config_consul_keys.append(
            consul.KeysKeyArgs(
                path="redash/datasource_configs/mitxonline-production/db_host",
                value=mitxonline_stack.require_output("mitxonline")["rds_host"],
            )
        )
        datasource_config_consul_keys.append(
            consul.KeysKeyArgs(
                path="redash/datasource_configs/odl-video-service-production/db_host",
                value=odl_video_service_stack.require_output("odl_video_service")[
                    "rds_host"
                ],
            )
        )
        datasource_config_consul_keys.append(
            consul.KeysKeyArgs(
                path="redash/datasource_configs/ocw-studio-production/db_host",
                value=ocw_studio_stack.require_output("ocw_studio_app")["rds_host"],
            )
        )
        datasource_config_consul_keys.append(
            consul.KeysKeyArgs(
                path="redash/datasource_configs/xpro-pg-production/db_host",
                # MD 20230123 Can't find the pulumi stack associated with this datasource.  # noqa: E501
                value="production-apps-rds-postgres-mitxpro.cbnm7ajau6mi.us-east-1.rds.amazonaws.com",
            )
        )
        datasource_config_consul_keys.append(
            consul.KeysKeyArgs(
                path="redash/datasource_configs/bootcamps-pg-production/db_host",
                value=bootcamps_stack.require_output("bootcamps_app")["rds_host"],
            )
        )
        datasource_config_consul_keys.append(
            consul.KeysKeyArgs(
                path="redash/datasource_configs/micromasters-pg-production/db_host",
                value=micromasters_stack.require_output("micromasters_app")["rds_host"],
            )
        )
    consul.Keys(
        "redash-datasource-config-template-data",
        keys=datasource_config_consul_keys,
        opts=consul_provider,
    )


block_device_mappings = [BlockDeviceMapping()]

# Setup the web ASG
web_lb_config = OLLoadBalancerConfig(
    subnets=data_vpc["subnet_ids"],
    security_groups=[redash_instance_security_group],
    tags=aws_config.merged_tags({"Name": web_tag}),
)

web_tg_config = OLTargetGroupConfig(
    vpc_id=data_vpc["id"],
    health_check_interval=60,
    health_check_matcher="200",
    health_check_path="/ping",
    stickiness="lb_cookie",
    tags=aws_config.merged_tags({"Name": web_tag}),
)

web_lt_config = OLLaunchTemplateConfig(
    block_device_mappings=block_device_mappings,
    image_id=redash_ami.id,
    instance_type=web_instance_type,
    instance_profile_arn=redash_instance_profile.arn,
    security_groups=[
        data_vpc["security_groups"]["default"],
        data_vpc["security_groups"]["web"],
        data_vpc["security_groups"]["integrator"],
        redash_instance_security_group.id,
    ],
    tags=aws_config.merged_tags({"Name": web_tag}),
    tag_specifications=[
        TagSpecification(
            resource_type="instance",
            tags=aws_config.merged_tags({"Name": web_tag}),
        ),
        TagSpecification(
            resource_type="volume",
            tags=aws_config.merged_tags({"Name": web_tag}),
        ),
    ],
    user_data=consul_datacenter.apply(
        lambda consul_dc: base64.b64encode(
            "#cloud-config\n{}".format(
                yaml.dump(
                    {
                        "write_files": [
                            {
                                "path": "/etc/default/docker-compose",
                                "content": textwrap.dedent(
                                    """\
                            COMPOSE_PROFILES=web
                                    """
                                ),
                                "owner": "root:root",
                            },
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
                                "path": "/etc/default/consul-template",
                                "content": f"ENVIRONMENT={consul_dc}",
                            },
                            {
                                "path": "/etc/default/vector",
                                "content": textwrap.dedent(
                                    f"""\
                            ENVIRONMENT={consul_dc}
                            APPLICATION=redash
                            SERVICE=data-platform
                            VECTOR_CONFIG_DIR=/etc/vector/
                            VECTOR_STRICT_ENV_VARS=false
                            AWS_REGION={aws_config.region}
                            GRAFANA_CLOUD_API_KEY={grafana_credentials["api_key"]}
                            GRAFANA_CLOUD_PROMETHEUS_API_USER={grafana_credentials["prometheus_user_id"]}
                            GRAFANA_CLOUD_LOKI_API_USER={grafana_credentials["loki_user_id"]}
                            """
                                ),
                                "owner": "root:root",
                            },
                        ]
                    },
                    sort_keys=True,
                )
            ).encode("utf8")
        ).decode("utf8")
    ),
)

web_autoscale_sizes = redash_config.get_object("auto_scale") or {
    "desired": 2,
    "min": 1,
    "max": 3,
}
web_asg_config = OLAutoScaleGroupConfig(
    asg_name=f"redash-server-web-{stack_info.env_suffix}",
    aws_config=aws_config,
    desired_size=web_autoscale_sizes["desired"],
    min_size=web_autoscale_sizes["min"],
    max_size=web_autoscale_sizes["max"],
    vpc_zone_identifiers=data_vpc["subnet_ids"],
    tags=aws_config.merged_tags({"Name": web_tag}),
)

web_as_setup = OLAutoScaling(
    asg_config=web_asg_config,
    lt_config=web_lt_config,
    tg_config=web_tg_config,
    lb_config=web_lb_config,
)


# Setup the worker ASG
worker_lt_config = OLLaunchTemplateConfig(
    block_device_mappings=block_device_mappings,
    image_id=redash_ami.id,
    instance_type=worker_instance_type,
    instance_profile_arn=redash_instance_profile.arn,
    security_groups=[
        data_vpc["security_groups"]["default"],
        data_vpc["security_groups"]["integrator"],
        redash_instance_security_group.id,
    ],
    tags=aws_config.merged_tags({"Name": worker_tag}),
    tag_specifications=[
        TagSpecification(
            resource_type="instance",
            tags=aws_config.merged_tags({"Name": worker_tag}),
        ),
        TagSpecification(
            resource_type="volume",
            tags=aws_config.merged_tags({"Name": worker_tag}),
        ),
    ],
    user_data=consul_datacenter.apply(
        lambda consul_dc: base64.b64encode(
            "#cloud-config\n{}".format(
                yaml.dump(
                    {
                        "write_files": [
                            {
                                "path": "/etc/default/docker-compose",
                                "content": textwrap.dedent(
                                    """\
                            COMPOSE_PROFILES=worker
                                    """
                                ),
                                "owner": "root:root",
                            },
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
                            VECTOR_STRICT_ENV_VARS=false
                            AWS_REGION={aws_config.region}
                            GRAFANA_CLOUD_API_KEY={grafana_credentials["api_key"]}
                            GRAFANA_CLOUD_PROMETHEUS_API_USER={grafana_credentials["prometheus_user_id"]}
                            GRAFANA_CLOUD_LOKI_API_USER={grafana_credentials["loki_user_id"]}
                            """
                                ),
                                "owner": "root:root",
                            },
                        ]
                    },
                    sort_keys=True,
                )
            ).encode("utf8")
        ).decode("utf8")
    ),
)

worker_autoscale_sizes = redash_config.get_object("auto_scale") or {
    "desired": 1,
    "min": 1,
    "max": 2,
}
worker_asg_config = OLAutoScaleGroupConfig(
    asg_name=f"redash-server-worker-{stack_info.env_suffix}",
    aws_config=aws_config,
    desired_size=worker_autoscale_sizes["desired"],
    min_size=worker_autoscale_sizes["min"],
    max_size=worker_autoscale_sizes["max"],
    vpc_zone_identifiers=data_vpc["subnet_ids"],
    tags=aws_config.merged_tags({"Name": worker_tag}),
)

worker_as_setup = OLAutoScaling(
    asg_config=worker_asg_config,
    lt_config=worker_lt_config,
    tg_config=None,
    lb_config=None,
)

fifteen_minutes = 60 * 15
redash_domain = route53.Record(
    f"redash-{stack_info.env_suffix}-service-domain",
    name=redash_config.require("domain"),
    type="CNAME",
    ttl=fifteen_minutes,
    records=[web_as_setup.load_balancer.dns_name],
    zone_id=mitodl_zone_id,
)

export(
    "redash_app",
    {
        "rds_host": redash_db.db_instance.address,
        "redis_cluster": redash_redis_cluster.address,
    },
)
