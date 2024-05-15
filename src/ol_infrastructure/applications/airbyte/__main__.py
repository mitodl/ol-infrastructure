"""Create the resources needed to run a airbyte server.  # noqa: D200"""

import base64
import json
import textwrap
from pathlib import Path

import pulumi_consul as consul
import pulumi_vault as vault
import yaml
from bridge.lib.magic_numbers import (
    AWS_RDS_DEFAULT_DATABASE_CAPACITY,
    DEFAULT_HTTPS_PORT,
    DEFAULT_POSTGRES_PORT,
)
from bridge.secrets.sops import read_yaml_secrets
from pulumi import Config, Output, ResourceOptions, StackReference, export
from pulumi_aws import ec2, get_caller_identity, iam, route53, s3
from pulumi_consul import Node, Service, ServiceCheckArgs

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
from ol_infrastructure.lib.aws.ec2_helper import InstanceTypes, default_egress_args
from ol_infrastructure.lib.aws.iam_helper import IAM_POLICY_VERSION, lint_iam_policy
from ol_infrastructure.lib.consul import get_consul_provider
from ol_infrastructure.lib.ol_types import AWSBase
from ol_infrastructure.lib.pulumi_helper import parse_stack
from ol_infrastructure.lib.stack_defaults import defaults
from ol_infrastructure.lib.vault import setup_vault_provider

##################################
##    Setup + Config Retrival   ##
##################################

if Config("vault_server").get("env_namespace"):
    setup_vault_provider()
stack_info = parse_stack()
airbyte_config = Config("airbyte")
network_stack = StackReference(f"infrastructure.aws.network.{stack_info.name}")
policy_stack = StackReference("infrastructure.aws.policies")
dns_stack = StackReference("infrastructure.aws.dns")
consul_stack = StackReference(f"infrastructure.consul.data.{stack_info.name}")
vault_stack = StackReference(f"infrastructure.vault.operations.{stack_info.name}")

mitodl_zone_id = dns_stack.require_output("odl_zone_id")

env_name = f"{stack_info.env_prefix}-{stack_info.env_suffix}"

target_vpc_name = airbyte_config.get("target_vpc") or f"{stack_info.env_prefix}_vpc"
target_vpc = network_stack.require_output(target_vpc_name)

consul_security_groups = consul_stack.require_output("security_groups")
aws_config = AWSBase(
    tags={
        "OU": airbyte_config.get("business_unit") or "operations",
        "Environment": f"{env_name}",
    }
)
aws_account = get_caller_identity()
vpc_id = target_vpc["id"]
airbyte_server_ami = ec2.get_ami(
    filters=[
        ec2.GetAmiFilterArgs(name="name", values=["airbyte-server-*"]),
        ec2.GetAmiFilterArgs(name="virtualization-type", values=["hvm"]),
        ec2.GetAmiFilterArgs(name="root-device-type", values=["ebs"]),
    ],
    most_recent=True,
    owners=[aws_account.account_id],
)

airbyte_server_tag = f"airbyte-server-{env_name}"
consul_provider = get_consul_provider(stack_info)

###############################
##     General Resources     ##
###############################

# S3 State Storage for Airbyte logs and system state
airbyte_bucket_name = f"ol-airbyte-{stack_info.env_suffix}"
s3.BucketV2(
    "airbyte-state-storage-bucket",
    bucket=airbyte_bucket_name,
    tags=aws_config.tags,
)

# IAM and instance profile
airbyte_server_instance_role = iam.Role(
    f"airbyte-server-instance-role-{env_name}",
    assume_role_policy=json.dumps(
        {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Action": "sts:AssumeRole",
                    "Principal": {"Service": "ec2.amazonaws.com"},
                },
            ],
        }
    ),
    path="/ol-infrastructure/airbyte-server/role/",
    tags=aws_config.tags,
)

parliament_config = {
    "PERMISSIONS_MANAGEMENT_ACTIONS": {
        "ignore_locations": [{"actions": ["s3:putobjectacl"]}]
    },
    "UNKNOWN_ACTION": {"ignore_locations": []},
}

airbyte_app_policy_document = {
    "Version": IAM_POLICY_VERSION,
    "Statement": [
        {
            "Effect": "Allow",
            "Action": [
                "s3:GetObject*",
                "s3:PutObject*",
                "s3:DeleteObject",
            ],
            "Resource": [f"arn:aws:s3:::{airbyte_bucket_name}/*"],
        },
        {
            "Effect": "Allow",
            "Action": "s3:ListBucket",
            "Resource": f"arn:aws:s3:::{airbyte_bucket_name}",
        },
    ],
}
airbyte_app_policy = iam.Policy(
    "airbyte-app-instance-iam-policy",
    path=f"/ol-applications/airbyte-server/{stack_info.env_prefix}/{stack_info.env_suffix}/",
    description=(
        "Grant access to AWS resources for the operation of the Airbyte application."
    ),
    policy=lint_iam_policy(
        airbyte_app_policy_document, stringify=True, parliament_config=parliament_config
    ),
    tags=aws_config.tags,
)

data_lake_policy_document = {
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
                f"arn:aws:s3:::ol-data-lake-*-{stack_info.env_suffix}",
                f"arn:aws:s3:::ol-data-lake-*-{stack_info.env_suffix}/*",
            ],
        },
        {
            "Effect": "Allow",
            "Action": [
                "glue:TagResource",
                "glue:UnTagResource",
            ],
            "Resource": ["*"],
        },
        {
            "Effect": "Allow",
            "Action": [
                "glue:BatchCreatePartition",
                "glue:BatchDeletePartition",
                "glue:BatchDeleteTable",
                "glue:BatchGetPartition",
                "glue:CreateTable",
                "glue:CreatePartition",
                "glue:DeletePartition",
                "glue:DeleteTable",
                "glue:GetDatabase",
                "glue:GetDatabases",
                "glue:GetPartition",
                "glue:GetPartitions",
                "glue:GetTable",
                "glue:GetTables",
                "glue:UpdateDatabase",
                "glue:UpdatePartition",
                "glue:UpdateTable",
            ],
            "Resource": [
                "arn:aws:glue:*:*:catalog",
                f"arn:aws:glue:*:*:database/*{stack_info.env_suffix}*",
                f"arn:aws:glue:*:*:table/*{stack_info.env_suffix}*/*",
            ],
        },
    ],
}
data_lake_policy = iam.Policy(
    "data-lake-access-policy",
    name_prefix="airbyte-datalake-policy-",
    path=f"/ol-applications/airbyte-server/{stack_info.env_prefix}/{stack_info.env_suffix}/",
    policy=lint_iam_policy(
        data_lake_policy_document,
        stringify=True,
        parliament_config=parliament_config,
    ),
    description="AWS access permissions to allow airbyte to use ol-data-lake-* buckets",
)

# Create IAM policy for Airbyte to read from S3 source buckets
# TODO: Turn this into a stack reference after exporting the bucket names from the  # noqa: E501, FIX002, TD002, TD003
# edxapp Pulumi project. (TMM 2023-06-02)
s3_source_buckets = [
    f"{edxapp_deployment}-{stack_info.env_suffix}-edxapp-tracking"
    for edxapp_deployment in ("mitxonline", "mitx", "mitx-staging", "xpro")
]
s3_source_buckets.append(f"ol-data-lake-landing-zone-{stack_info.env_suffix}")

# This should use a reference to the monitoring stack but it seems broken at the moment
# and I can't figure it out
fastly_access_log_bucket_name = "mitodl-fastly-access-logs"
s3_source_buckets.append(fastly_access_log_bucket_name)

s3_source_policy_document = {
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
                "s3:ListBucket*",
            ],
            "Resource": [
                f"arn:aws:s3:::{bucket_name}" for bucket_name in s3_source_buckets
            ]
            + [f"arn:aws:s3:::{bucket_name}/*" for bucket_name in s3_source_buckets],
        },
    ],
}
s3_source_policy = iam.Policy(
    "airbyte-s3-source-access-policy",
    name_prefix="airbyte-s3-source-policy-",
    path=f"/ol-applications/airbyte-server/{stack_info.env_prefix}/{stack_info.env_suffix}/",
    policy=lint_iam_policy(
        s3_source_policy_document,
        stringify=True,
    ),
    description="AWS access permissions to access S3 buckets for data sources",
)

iam.RolePolicyAttachment(
    f"airbyte-server-describe-instance-role-policy-{env_name}",
    policy_arn=policy_stack.require_output("iam_policies")["describe_instances"],
    role=airbyte_server_instance_role.name,
)
iam.RolePolicyAttachment(
    f"airbyte-server-route53-role-policy-{env_name}",
    policy_arn=policy_stack.require_output("iam_policies")["route53_odl_zone_records"],
    role=airbyte_server_instance_role.name,
)
iam.RolePolicyAttachment(
    f"airbyte-server-data-lake-access-policy-{env_name}",
    policy_arn=data_lake_policy.arn,
    role=airbyte_server_instance_role.name,
)
iam.RolePolicyAttachment(
    f"airbyte-server-s3-source-access-polic-{env_name}",
    policy_arn=s3_source_policy.arn,
    role=airbyte_server_instance_role.name,
)
iam.RolePolicyAttachment(
    "airbyte-app-instance-policy-attachement",
    policy_arn=airbyte_app_policy.arn,
    role=airbyte_server_instance_role.name,
)

airbyte_server_instance_profile = iam.InstanceProfile(
    f"airbyte-server-instance-profile-{env_name}",
    role=airbyte_server_instance_role.name,
    path="/ol-infrastructure/airbyte-server/profile/",
)

airbyte_lakeformation_role = iam.Role(
    "airbyte-lakeformation-role",
    assume_role_policy=airbyte_server_instance_role.arn.apply(
        lambda instance_role_arn: json.dumps(
            {
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Effect": "Allow",
                        "Action": "sts:AssumeRole",
                        "Principal": {"AWS": instance_role_arn},
                    },
                ],
            }
        )
    ),
    name=f"airbyte-lakeformation-role-{stack_info.env_suffix}",
    path="/ol-infrastructure/airbyte-app/role/",
    tags=aws_config.tags,
)

iam.RolePolicyAttachment(
    f"airbyte-application-lakeformation-access-policy-{env_name}",
    policy_arn=data_lake_policy.arn,
    role=airbyte_lakeformation_role.name,
)

# Vault policy definition
airbyte_server_vault_policy = vault.Policy(
    "airbyte-server-vault-policy",
    name="airbyte-server",
    policy=Path(__file__).parent.joinpath("airbyte_server_policy.hcl").read_text(),
)
# Register Airbyte AMI for Vault AWS auth
vault.aws.AuthBackendRole(
    "airbyte-server-ami-ec2-vault-auth",
    backend="aws",
    auth_type="iam",
    role="airbyte-server",
    inferred_entity_type="ec2_instance",
    inferred_aws_region=aws_config.region,
    bound_iam_instance_profile_arns=[airbyte_server_instance_profile.arn],
    bound_ami_ids=[airbyte_server_ami.id],
    bound_account_ids=[aws_account.account_id],
    bound_vpc_ids=[vpc_id],
    token_policies=[airbyte_server_vault_policy.name],
)

# Create the secret mount used for storing configuration secrets
airbyte_vault_mount = vault.Mount(
    "airbyte-server-configuration-secrets-mount",
    path="secret-airbyte",
    type="kv-v2",
    options={"version": 2},
    description="Storage of configuration credentials used in Airbyte connections.",
    opts=ResourceOptions(delete_before_replace=True),
)

# Define a Vault role that can be used to generate credentials for the S3 source policy
vault.aws.SecretBackendRole(
    "airbyte-s3-source-vault-aws-role",
    name="airbyte-sources",
    backend="aws-mitx",
    credential_type="iam_user",
    policy_arns=[s3_source_policy.arn],
)

airbyte_vault_secrets = read_yaml_secrets(
    Path(f"airbyte/data.{stack_info.env_suffix}.yaml")
)

vault.generic.Secret(
    "airbyte-server-configuration-sentry-secrets",
    path=airbyte_vault_mount.path.apply("{}/sentry-dsn".format),
    data_json=json.dumps(airbyte_vault_secrets["sentry-dsn"]),
)
##################################
#     Network Access Control     #
##################################
# Create security group
airbyte_server_security_group = ec2.SecurityGroup(
    f"airbyte-server-security-group-{env_name}",
    name=f"airbyte-server-{target_vpc_name}-{env_name}",
    description="Access control for airbyte servers",
    ingress=[
        ec2.SecurityGroupIngressArgs(
            protocol="tcp",
            from_port=DEFAULT_HTTPS_PORT,
            to_port=DEFAULT_HTTPS_PORT,
            cidr_blocks=["0.0.0.0/0"],
            description=(
                f"Allow traffic to the airbyte server on port {DEFAULT_HTTPS_PORT}"
            ),
        ),
    ],
    egress=default_egress_args,
    vpc_id=vpc_id,
)

airbyte_db_security_group = ec2.SecurityGroup(
    f"airbyte-db-security-group-{env_name}",
    name=f"airbyte-db-{target_vpc_name}-{env_name}",
    description="Access from airbyte to its own postgres database.",
    ingress=[
        ec2.SecurityGroupIngressArgs(
            security_groups=[
                airbyte_server_security_group.id,
                consul_security_groups["consul_server"],
                vault_stack.require_output("vault_server")["security_group"],
            ],
            cidr_blocks=[target_vpc["cidr"]],
            protocol="tcp",
            from_port=DEFAULT_POSTGRES_PORT,
            to_port=DEFAULT_POSTGRES_PORT,
            description="Access to Postgres from Airbyte nodes.",
        ),
    ],
    tags=aws_config.tags,
    vpc_id=vpc_id,
)

#########################
#     Database Setup    #
#########################
rds_defaults = defaults(stack_info)["rds"]
rds_defaults["instance_size"] = (
    airbyte_config.get("db_instance_size") or rds_defaults["instance_size"]
)

rds_password = airbyte_config.require("rds_password")

airbyte_db_config = OLPostgresDBConfig(
    instance_name=f"airbyte-db-{stack_info.env_suffix}",
    password=rds_password,
    storage=airbyte_config.get("db_capacity") or str(AWS_RDS_DEFAULT_DATABASE_CAPACITY),
    subnet_group_name=target_vpc["rds_subnet"],
    security_groups=[airbyte_db_security_group],
    parameter_overrides=[{"name": "rds.force_ssl", "value": 0}],
    engine_major_version="13",
    tags=aws_config.tags,
    db_name="airbyte",
    **rds_defaults,
)
airbyte_db = OLAmazonDB(airbyte_db_config)

# Shorten a few frequently used attributes from the database
db_address = airbyte_db.db_instance.address
db_port = airbyte_db.db_instance.port
db_name = airbyte_db.db_instance.db_name

airbyte_db_vault_backend_config = OLVaultPostgresDatabaseConfig(
    db_name=airbyte_db_config.db_name,
    mount_point=f"{airbyte_db_config.engine}-airbyte",
    db_admin_username=airbyte_db_config.username,
    db_admin_password=rds_password,
    db_host=airbyte_db.db_instance.address,
)
airbyte_db_vault_backend = OLVaultDatabaseBackend(airbyte_db_vault_backend_config)

airbyte_db_consul_node = Node(
    "airbyte-instance-db-node",
    name="airbyte-postgres-db",
    address=db_address,
    opts=consul_provider,
)

airbyte_db_consul_service = Service(
    "airbyte-instance-db-service",
    node=airbyte_db_consul_node.name,
    name="airbyte-postgres",
    port=db_port,
    meta={
        "external-node": True,
        "external-probe": True,
    },
    checks=[
        ServiceCheckArgs(
            check_id="airbyte-instance-db",
            interval="10s",
            name="airbyte-instance-db",
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

connection_string = Output.all(address=db_address, port=db_port, name=db_name).apply(
    lambda db: (
        "jdbc:postgresql://{address}:{port}/{name}?ssl=true&sslmode=require".format(
            **db
        )
    )
)

consul.Keys(
    "airbyte-consul-template-data",
    keys=[
        consul.KeysKeyArgs(path="airbyte/database-host", value=db_address),
        consul.KeysKeyArgs(path="airbyte/database-port", value=db_port),
        consul.KeysKeyArgs(path="airbyte/database-name", value=db_name),
        consul.KeysKeyArgs(
            path="airbyte/database-connection-string",
            value=connection_string,
        ),
        consul.KeysKeyArgs(
            path="airbyte/vault-address",
            value=f"{Config('vault').get('address')}/",
        ),
        consul.KeysKeyArgs(
            path="airbyte/airbyte-hostname",
            value=airbyte_config.require("web_host_domain"),
        ),
        consul.KeysKeyArgs(
            path="airbyte/traefik-certificate-resolver",
            value=(
                "letsencrypt_staging_resolver"
                if stack_info.env_suffix != "production"
                else "letsencrypt_resolver"
            ),
        ),
        consul.KeysKeyArgs(path="airbyte/env-stage", value=stack_info.env_suffix),
    ],
    opts=consul_provider,
)

###################################
#     Web Node EC2 Deployment     #
###################################
lb_config = OLLoadBalancerConfig(
    subnets=target_vpc["subnet_ids"],
    security_groups=[airbyte_server_security_group],
    # Give extra time for discover_schema calls in connection setup
    idle_timeout_seconds=60 * 5,
    tags=aws_config.merged_tags({"Name": airbyte_server_tag}),
)

tg_config = OLTargetGroupConfig(
    vpc_id=vpc_id,
    health_check_interval=60,
    health_check_matcher="200-399",
    health_check_path="/api/health",
    health_check_unhealthy_threshold=6,  # give extra time for airbyte to start up
    tags=aws_config.merged_tags({"Name": airbyte_server_tag}),
)

consul_datacenter = consul_stack.require_output("datacenter")
grafana_credentials = read_yaml_secrets(
    Path(f"vector/grafana.{stack_info.env_suffix}.yaml")
)

block_device_mappings = [BlockDeviceMapping(volume_size=50)]
tag_specs = [
    TagSpecification(
        resource_type="instance",
        tags=aws_config.merged_tags({"Name": airbyte_server_tag}),
    ),
    TagSpecification(
        resource_type="volume",
        tags=aws_config.merged_tags({"Name": airbyte_server_tag}),
    ),
]

lt_config = OLLaunchTemplateConfig(
    block_device_mappings=block_device_mappings,
    image_id=airbyte_server_ami.id,
    instance_type=airbyte_config.get("instance_type") or InstanceTypes.burstable_medium,
    instance_profile_arn=airbyte_server_instance_profile.arn,
    security_groups=[
        airbyte_server_security_group,
        consul_security_groups["consul_agent"],
        target_vpc["security_groups"]["integrator"],
    ],
    tags=aws_config.merged_tags({"Name": airbyte_server_tag}),
    tag_specifications=tag_specs,
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
                            APPLICATION=air-byte
                            SERVICE=data-platform
                            VECTOR_CONFIG_DIR=/etc/vector/
                            VECTOR_STRICT_ENV_VARS=false
                            AWS_REGION={aws_config.region}
                            GRAFANA_CLOUD_API_KEY={grafana_credentials['api_key']}
                            GRAFANA_CLOUD_PROMETHEUS_API_USER={grafana_credentials['prometheus_user_id']}
                            GRAFANA_CLOUD_LOKI_API_USER={grafana_credentials['loki_user_id']}
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

auto_scale_config = airbyte_config.get_object("auto_scale") or {
    "desired": 1,
    "min": 1,
    "max": 2,
}
asg_config = OLAutoScaleGroupConfig(
    asg_name=f"airbyte-server-{env_name}",
    aws_config=aws_config,
    desired_size=auto_scale_config["desired"],
    min_size=auto_scale_config["min"],
    max_size=auto_scale_config["max"],
    vpc_zone_identifiers=target_vpc["subnet_ids"],
    tags=aws_config.merged_tags({"Name": airbyte_server_tag}),
)

as_setup = OLAutoScaling(
    asg_config=asg_config,
    lt_config=lt_config,
    tg_config=tg_config,
    lb_config=lb_config,
)

## Create Route53 DNS records for airbyte
five_minutes = 60 * 5
route53.Record(
    "airbyte-server-dns-record",
    name=airbyte_config.require("web_host_domain"),
    type="CNAME",
    ttl=five_minutes,
    records=[as_setup.load_balancer.dns_name],
    zone_id=mitodl_zone_id,
)
route53.Record(
    "airbyte-api-server-dns-record",
    name=f"api-{airbyte_config.require('web_host_domain')}",
    type="CNAME",
    ttl=five_minutes,
    records=[as_setup.load_balancer.dns_name],
    zone_id=mitodl_zone_id,
)
route53.Record(
    "airbyte-config-api-dns-record",
    name=f"config-{airbyte_config.require('web_host_domain')}",
    type="CNAME",
    ttl=five_minutes,
    records=[as_setup.load_balancer.dns_name],
    zone_id=mitodl_zone_id,
)
route53.Record(
    "airbyte-auth-dns-record",
    name=airbyte_config.require("auth_domain"),
    type="CNAME",
    ttl=five_minutes,
    records=[as_setup.load_balancer.dns_name],
    zone_id=mitodl_zone_id,
)

export("lakeformation_role_arn", airbyte_lakeformation_role.arn)
