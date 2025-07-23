"""The complete state necessary to deploy an instance of the Dagster application.

- Create an S3 bucket for managing Dagster's intermediate states
- Create an RDS PostgreSQL instance for storing Dagster's schedule and run state
- Mount a Vault database backend and provision role definitions for the Dagster RDS database
- Create an IAM role for Dagster instances to allow access to S3 and other AWS resources
- Setup consul keys needed by various pipelines.
- Provision an EC2 instance from a pre-built AMI with the pipeline code for Dagster
"""  # noqa: E501

import base64
import json
import textwrap
from pathlib import Path
from typing import Any

import pulumi_consul as consul
import pulumi_vault as vault
import yaml
from pulumi import ResourceOptions, StackReference, export
from pulumi.config import get_config
from pulumi_aws import ec2, get_caller_identity, iam, route53, s3
from pulumi_consul import Node, Service, ServiceCheckArgs

from bridge.lib.magic_numbers import DEFAULT_POSTGRES_PORT
from bridge.secrets.sops import read_yaml_secrets
from ol_infrastructure.components.aws.database import OLAmazonDB, OLPostgresDBConfig
from ol_infrastructure.components.services.vault import (
    OLVaultDatabaseBackend,
    OLVaultPostgresDatabaseConfig,
)
from ol_infrastructure.lib.aws.ec2_helper import DiskTypes
from ol_infrastructure.lib.aws.iam_helper import lint_iam_policy
from ol_infrastructure.lib.consul import get_consul_provider
from ol_infrastructure.lib.ol_types import AWSBase
from ol_infrastructure.lib.pulumi_helper import parse_stack
from ol_infrastructure.lib.stack_defaults import defaults
from ol_infrastructure.lib.vault import setup_vault_provider

setup_vault_provider()
stack_info = parse_stack()
dns_stack = StackReference("infrastructure.aws.dns")
network_stack = StackReference(f"infrastructure.aws.network.{stack_info.name}")
policy_stack = StackReference("infrastructure.aws.policies")
vault_stack = StackReference(f"infrastructure.vault.operations.{stack_info.name}")
consul_stack = StackReference(f"infrastructure.consul.data.{stack_info.name}")
mitodl_zone_id = dns_stack.require_output("odl_zone_id")
data_vpc = network_stack.require_output("data_vpc")
operations_vpc = network_stack.require_output("operations_vpc")
dagster_environment = f"data-{stack_info.env_suffix}"
aws_config = AWSBase(
    tags={"OU": "data", "Environment": dagster_environment},
)
consul_provider = get_consul_provider(stack_info)

consul_security_groups = consul_stack.require_output("security_groups")
aws_account = get_caller_identity()

mitxonline_stack = StackReference(f"applications.edxapp.mitxonline.{stack_info.name}")
mitxonline_mongodb_stack = StackReference(
    f"infrastructure.mongodb_atlas.mitxonline.{stack_info.name}"
)
residential_mongodb_stack = StackReference(
    f"infrastructure.mongodb_atlas.mitx.{stack_info.name}"
)
xpro_stack = StackReference(f"applications.edxapp.xpro.{stack_info.name}")
xpro_mongodb_stack = StackReference(
    f"infrastructure.mongodb_atlas.xpro.{stack_info.name}"
)

dagster_bucket_name = f"dagster-{dagster_environment}"
s3_tracking_logs_buckets = [
    f"{edxapp_deployment}-{stack_info.env_suffix}-edxapp-tracking"
    for edxapp_deployment in ("mitxonline", "mitx", "mitx-staging", "xpro")
]
dagster_s3_permissions: list[dict[str, str | list[str]]] = [
    {
        "Effect": "Allow",
        "Action": "s3:ListAllMyBuckets",
        "Resource": "*",
    },
    {
        "Effect": "Allow",
        "Action": [
            "s3:ListBucket*",
            "s3:GetObject",
            "s3:PutObject",
            "s3:DeleteObject*",
        ],
        "Resource": [
            f"arn:aws:s3:::{dagster_bucket_name}",
            f"arn:aws:s3:::{dagster_bucket_name}/*",
        ],
    },
    {
        "Effect": "Allow",
        "Action": [
            "s3:ListBucket*",
            "s3:GetObject",
            "s3:PutObject",
            "s3:DeleteObject*",
        ],
        "Resource": ["arn:aws:s3:::mitx-etl*", "arn:aws:s3:::mitx-etl*/*"],
    },
    {
        "Effect": "Allow",
        "Action": [
            "s3:ListBucket*",
            "s3:GetObject",
            "s3:PutObject",
            "s3:DeleteObject*",
        ],
        "Resource": [
            f"arn:aws:s3:::ol-data-lake-*-{stack_info.env_suffix}",
            f"arn:aws:s3:::ol-data-lake-*-{stack_info.env_suffix}/*",
        ],
    },
    {
        "Effect": "Allow",
        "Action": [
            "s3:GetBucketLocation",
            "s3:GetObject",
            "s3:ListBucket",
            "s3:PutObject",
        ],
        "Resource": [
            f"arn:aws:s3:::ol-warehouse-results-{stack_info.env_suffix}",
            f"arn:aws:s3:::ol-warehouse-results-{stack_info.env_suffix}/*",
        ],
    },
    {
        "Effect": "Allow",
        "Action": [
            "s3:GetBucketLocation",
            "s3:GetObject",
            "s3:ListBucket",
            "s3:PutObject",
            "s3:DeleteObject",
        ],
        "Resource": [
            f"arn:aws:s3:::*-{stack_info.env_suffix}-edxapp-courses",
            f"arn:aws:s3:::*-{stack_info.env_suffix}-edxapp-courses/*",
        ],
    },
    {
        "Effect": "Allow",
        "Action": [
            "s3:GetBucketLocation",
            "s3:GetObject*",
            "s3:ListBucket",
            "s3:PutObject",
        ],
        "Resource": [
            f"arn:aws:s3:::{bucket_name}" for bucket_name in s3_tracking_logs_buckets
        ]
        + [f"arn:aws:s3:::{bucket_name}/*" for bucket_name in s3_tracking_logs_buckets],
    },
]

athena_permissions: list[dict[str, str | list[str]]] = [
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
            f"arn:aws:glue:*:*:database/*{stack_info.env_suffix}",
            f"arn:aws:glue:*:*:table/*{stack_info.env_suffix}/*",
        ],
    },
]

edxorg_program_credentials_role_assumption = {
    "Effect": "Allow",
    "Action": ["sts:AssumeRole"],
    "Resource": "arn:aws:iam::708756755355:role/mit-s3-edx-program-reports-access",
}

dagster_iam_permissions = {
    "Version": "2012-10-17",
    "Statement": [
        *dagster_s3_permissions,
        *athena_permissions,
        edxorg_program_credentials_role_assumption,
    ],
}

parliament_config: dict[str, Any] = {
    "RESOURCE_EFFECTIVELY_STAR": {"ignore_locations": []},
    "CREDENTIALS_EXPOSURE": {"ignore_locations": [{"actions": "sts:assumeRole"}]},
}

dagster_runtime_bucket = s3.Bucket(
    dagster_bucket_name,
    bucket=dagster_bucket_name,
    acl="private",
    tags=aws_config.tags,
    versioning={"enabled": True},
    server_side_encryption_configuration={
        "rule": {
            "applyServerSideEncryptionByDefault": {
                "sseAlgorithm": "aws:kms",
            },
        },
    },
)

# Bucket to store gcs import of edxorg course tarballs
edxorg_courses_bucket_name = f"edxorg-{stack_info.env_suffix}-edxapp-courses"
edxorg_courses_bucket = s3.Bucket(
    edxorg_courses_bucket_name,
    bucket=edxorg_courses_bucket_name,
    acl="private",
    tags=aws_config.tags,
    versioning={"enabled": True},
    server_side_encryption_configuration={
        "rule": {
            "applyServerSideEncryptionByDefault": {
                "sseAlgorithm": "aws:kms",
            },
        },
    },
)

# Create instance profile for granting access to S3 buckets
dagster_iam_policy = iam.Policy(
    f"dagster-policy-{stack_info.env_suffix}",
    name=f"dagster-policy-{stack_info.env_suffix}",
    path=f"/ol-data/etl-policy-{stack_info.env_suffix}/",
    policy=lint_iam_policy(
        dagster_iam_permissions, stringify=True, parliament_config=parliament_config
    ),
    description="Policy for granting acces for batch data workflows to AWS resources",
)

dagster_role = iam.Role(
    "etl-instance-role",
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
    name=f"etl-instance-role-{stack_info.env_suffix}",
    path="/ol-data/etl-role/",
    tags=aws_config.tags,
)

iam.RolePolicyAttachment(
    f"dagster-role-policy-{stack_info.env_suffix}",
    policy_arn=dagster_iam_policy.arn,
    role=dagster_role.name,
)

iam.RolePolicyAttachment(
    f"dagster-describe-instance-role-policy-{stack_info.env_suffix}",
    policy_arn=policy_stack.require_output("iam_policies")["describe_instances"],
    role=dagster_role.name,
)

dagster_profile = iam.InstanceProfile(
    f"dagster-instance-profile-{stack_info.env_suffix}",
    role=dagster_role.name,
    name=f"etl-instance-profile-{stack_info.env_suffix}",
    path="/ol-data/etl-profile/",
)

dagster_db_security_group = ec2.SecurityGroup(
    f"dagster-db-access-{stack_info.env_suffix}",
    name=f"ol-etl-db-access-{stack_info.env_suffix}",
    description="Access from the data VPC to the Dagster database",
    ingress=[
        ec2.SecurityGroupIngressArgs(
            security_groups=[
                data_vpc["security_groups"]["orchestrator"],
                vault_stack.require_output("vault_server")["security_group"],
            ],
            protocol="tcp",
            from_port=DEFAULT_POSTGRES_PORT,
            to_port=DEFAULT_POSTGRES_PORT,
        )
    ],
    tags=aws_config.tags,
    vpc_id=data_vpc["id"],
)

rds_defaults = defaults(stack_info)["rds"]
rds_defaults["monitoring_profile_name"] = "disabled"

dagster_db_config = OLPostgresDBConfig(
    db_name="dagster",
    engine_major_version="16",
    instance_name=f"ol-etl-db-{stack_info.env_suffix}",
    max_storage=1000,
    password=get_config("dagster:db_password"),
    security_groups=[dagster_db_security_group],
    subnet_group_name=data_vpc["rds_subnet"],
    tags=aws_config.tags,
    **rds_defaults,
)
dagster_db = OLAmazonDB(dagster_db_config)

dagster_db_vault_backend_config = OLVaultPostgresDatabaseConfig(
    db_name=dagster_db_config.db_name,
    mount_point=f"{dagster_db_config.engine}-dagster",
    db_admin_username=dagster_db_config.username,
    db_admin_password=get_config("dagster:db_password"),
    db_host=dagster_db.db_instance.address,
)
dagster_db_vault_backend = OLVaultDatabaseBackend(dagster_db_vault_backend_config)

dagster_db_consul_node = Node(
    "dagster-instance-db-node",
    name="dagster-postgres-db",
    address=dagster_db.db_instance.address,
    datacenter=dagster_environment,
    opts=consul_provider,
)

dagster_db_consul_service = Service(
    "dagster-instance-db-service",
    node=dagster_db_consul_node.name,
    name="dagster-db",
    port=dagster_db_config.port,
    meta={
        "external-node": True,
        "external-probe": True,
    },
    checks=[
        ServiceCheckArgs(
            check_id="dagster-instance-db",
            interval="10s",
            name="dagster-instance-id",
            timeout="60s",
            status="passing",
            tcp=dagster_db.db_instance.address.apply(
                lambda address: f"{address}:{dagster_db_config.port}"
            ),
        )
    ],
    opts=consul_provider,
)

# Get the AMI ID for the dagster/docker-compose image
dagster_image = ec2.get_ami(
    filters=[
        ec2.GetAmiFilterArgs(name="name", values=["dagster-server-*"]),
        ec2.GetAmiFilterArgs(name="virtualization-type", values=["hvm"]),
        ec2.GetAmiFilterArgs(name="root-device-type", values=["ebs"]),
    ],
    most_recent=True,
    owners=[aws_account.account_id],
)

# Create a vault policy to allow dagster to get to the secrets it needs
dagster_server_vault_policy = vault.Policy(
    "dagster-server-vault-policy",
    name="dagster-server",
    policy=Path(__file__).parent.joinpath("dagster_server_policy.hcl").read_text(),
)
# Register Dagster AMI for Vault AWS auth
vault.aws.AuthBackendRole(
    "dagster-server-ami-ec2-vault-auth",
    backend="aws",
    auth_type="iam",
    role="dagster-server",
    inferred_entity_type="ec2_instance",
    inferred_aws_region=aws_config.region,
    bound_iam_instance_profile_arns=[dagster_profile.arn],
    bound_ami_ids=[
        dagster_image.id
    ],  # Reference the new way of doing stuff, not the old one
    bound_account_ids=[aws_account.account_id],
    bound_vpc_ids=[data_vpc["id"]],
    token_policies=[dagster_server_vault_policy.name],
)

# Create the consul keys required by various pipeline configurations
consul.Keys(
    "dagster-consul-template-data",
    keys=[
        consul.KeysKeyArgs(
            path="dagster/postgresql-host",
            value=dagster_db.db_instance.address,
        ),
        consul.KeysKeyArgs(
            path="dagster/dagster-bucket-name",
            value=f"dagster-data-{stack_info.env_suffix}",
        ),
        consul.KeysKeyArgs(
            path="dagster/server-address",
            value=get_config("dagster:domain"),
        ),
        consul.KeysKeyArgs(
            path="edx-pipeline/shared/env-suffix",
            value=stack_info.env_suffix,
        ),
        consul.KeysKeyArgs(
            path="edx-pipeline/mit-open/bucket-name",
            value=get_config("dagster:edx_pipeline_mit_open_bucket_name"),
        ),
        consul.KeysKeyArgs(
            path="edx-pipeline/mit-open/postgres-db/hostname",
            value=get_config("dagster:edx_pipeline_mit_open_postgres_db_hostname"),
        ),
        consul.KeysKeyArgs(
            path="edx-pipeline/xpro-edx/bucket-name",
            value=get_config("dagster:edx_pipeline_xpro_edx_bucket_name"),
        ),
        consul.KeysKeyArgs(
            path="edx-pipeline/xpro-edx/mysql-db/hostname",
            value=xpro_stack.require_output("edxapp")["mariadb"],
        ),
        consul.KeysKeyArgs(
            path="edx-pipeline/xpro-edx/mongodb-forum/uri",
            value=xpro_mongodb_stack.require_output("atlas_cluster")[
                "mongo_uri_with_options"
            ],
        ),
        consul.KeysKeyArgs(
            path="edx-pipeline/xpro-edx/xpro-purpose",
            value=get_config("dagster:edx_pipeline_xpro_edx_xpro_purpose"),
        ),
        consul.KeysKeyArgs(
            path="edx-pipeline/xpro-edx/xpro-course-bucket-name",
            value=get_config("dagster:edx_pipeline_xpro_edx_course_bucket_name"),
        ),
        consul.KeysKeyArgs(
            path="edx-pipeline/micromasters/postgres-db/name",
            value=get_config("dagster:edx_pipeline_micromasters_postgres_db_name"),
        ),
        consul.KeysKeyArgs(
            path="edx-pipeline/micromasters/postgres-db/hostname",
            value=get_config("dagster:edx_pipeline_micromasters_postgres_db_hostname"),
        ),
        consul.KeysKeyArgs(
            path="edx-pipeline/micromasters/bucket-name",
            value=get_config("dagster:edx_pipeline_micromasters_bucket_name"),
        ),
        consul.KeysKeyArgs(
            path="edx-pipeline-mitx-online/edx-course-bucket",
            value=get_config("dagster:edx_pipeline_mitx_online_edx_course_bucket"),
        ),
        consul.KeysKeyArgs(
            path="edx-pipeline/mitx-online/purpose",
            value=get_config("dagster:edx_pipeline_mitx_online_purpose"),
        ),
        consul.KeysKeyArgs(
            path="edx-pipeline/residential/mongodb/uri",
            value=residential_mongodb_stack.require_output("atlas_cluster")[
                "mongo_uri_with_options"
            ],
        ),
        consul.KeysKeyArgs(
            path="edx-pipeline/residential/mysql-db/hostname",
            value=get_config("dagster:edx_pipeline_residential_mysql_db_hostname"),
        ),
        consul.KeysKeyArgs(
            path="edx-pipeline/mitxonline/mysql-db/hostname",
            value=mitxonline_stack.require_output("edxapp")["mariadb"],
        ),
        consul.KeysKeyArgs(
            path="edx-pipeline/mitxonline/mongodb/uri",
            value=mitxonline_mongodb_stack.require_output("atlas_cluster")[
                "mongo_uri_with_options"
            ],
        ),
        consul.KeysKeyArgs(
            path="edx-pipeline/mitxonline/purpose",
            value=get_config("dagster:edx_pipeline_xpro_edx_xpro_purpose"),
        ),
        consul.KeysKeyArgs(
            path="edx-pipeline/mitxonline/edx-course-bucket-name",
            value=get_config("dagster:edx_pipeline_mitxonline_edx_course_bucket_name"),
        ),
        consul.KeysKeyArgs(
            path="edx-pipeline/mitx-enrollments/bucket-name",
            value=get_config("dagster:edx_pipeline_mitx_enrollments_bucket_name"),
        ),
    ],
    opts=consul_provider,
)

# Begin block for migrating to pyinfra images
grafana_credentials = read_yaml_secrets(
    Path(f"vector/grafana.{stack_info.env_suffix}.yaml")
)
consul_datacenter = consul_stack.require_output("datacenter")
instance_tags = aws_config.merged_tags(
    {"Name": f"dagster-instance-{stack_info.env_suffix}"}
)
dagster_instance = ec2.Instance(
    f"dagster-instance-{stack_info.env_suffix}",
    ami=dagster_image.id,
    instance_type=get_config("dagster:instance_type"),
    iam_instance_profile=dagster_profile.id,
    tags=instance_tags,
    volume_tags=instance_tags,
    subnet_id=data_vpc["subnet_ids"][1],
    key_name="oldevops",
    root_block_device=ec2.InstanceRootBlockDeviceArgs(
        volume_type=DiskTypes.ssd,
        volume_size=int(get_config("dagster:disk_size_gb") or 100),
    ),
    vpc_security_group_ids=[
        data_vpc["security_groups"]["default"],
        data_vpc["security_groups"]["web"],
        consul_security_groups["consul_agent"],
        data_vpc["security_groups"]["orchestrator"],
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
                            APPLICATION=dagster
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
                            {
                                "path": "/etc/default/consul-template",
                                "content": (
                                    f"DAGSTER_ENVIRONMENT={stack_info.env_suffix}"
                                ),
                            },
                        ]
                    },
                    sort_keys=True,
                )
            ).encode("utf8")
        ).decode("utf8")
    ),
    opts=ResourceOptions(depends_on=[dagster_db_consul_service]),
)
dagster_elastic_ip = ec2.Eip(
    "dagster-instance-elastic-ip",
    instance=dagster_instance.id,
)

fifteen_minutes = 60 * 15
dagster_domain = route53.Record(
    f"dagster-{stack_info.env_suffix}-service-domain",
    name=get_config("dagster:domain"),
    type="A",
    ttl=fifteen_minutes,
    records=[dagster_elastic_ip.public_ip],
    zone_id=mitodl_zone_id,
    opts=ResourceOptions(depends_on=[dagster_instance]),
)
dagster_domain_v6 = route53.Record(
    f"dagster-{stack_info.env_suffix}-service-domain-v6",
    name=get_config("dagster:domain"),
    type="AAAA",
    ttl=fifteen_minutes,
    records=dagster_instance.ipv6_addresses,
    zone_id=mitodl_zone_id,
    opts=ResourceOptions(depends_on=[dagster_instance]),
)

export(
    "dagster_app",
    {
        "rds_host": dagster_db.db_instance.address,
        "elastic_ip": dagster_elastic_ip.public_ip,
        "ec2_private_address": dagster_instance.private_ip,
        "ec2_public_address": dagster_instance.public_ip,
        "ec2_address_v6": dagster_instance.ipv6_addresses,
    },
)
