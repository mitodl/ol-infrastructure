"""The complete state necessary to deploy an instance of the Dagster application.

- Create an S3 bucket for managing Dagster's intermediate states
- Create an RDS PostgreSQL instance for storing Dagster's schedule and run state
- Mount a Vault database backend and provision role definitions for the Dagster RDS database
- Create an IAM role for Dagster instances to allow access to S3 and other AWS resources
- Register a minion ID and key pair with the appropriate SaltStack master instance
- Provision an EC2 instance from a pre-built AMI with the pipeline code for Dagster
"""
import json
from typing import Dict, List, Union

from pulumi import ResourceOptions, StackReference, export
from pulumi.config import get_config
from pulumi_aws import ec2, iam, route53, s3
from pulumi_consul import Node, Service, ServiceCheckArgs

from ol_infrastructure.components.aws.database import OLAmazonDB, OLPostgresDBConfig
from ol_infrastructure.components.services.vault import (
    OLVaultDatabaseBackend,
    OLVaultPostgresDatabaseConfig,
)
from ol_infrastructure.lib.aws.ec2_helper import DiskTypes, build_userdata
from ol_infrastructure.lib.aws.iam_helper import lint_iam_policy
from ol_infrastructure.lib.consul import get_consul_provider
from ol_infrastructure.lib.ol_types import AWSBase
from ol_infrastructure.lib.pulumi_helper import parse_stack
from ol_infrastructure.lib.stack_defaults import defaults
from ol_infrastructure.lib.vault import setup_vault_provider
from ol_infrastructure.providers.salt.minion import (
    OLSaltStackMinion,
    OLSaltStackMinionInputs,
)

setup_vault_provider()
stack_info = parse_stack()
data_warehouse_stack = StackReference(
    f"infrastructure.aws.data_warehouse.{stack_info.name}"
)
dns_stack = StackReference("infrastructure.aws.dns")
network_stack = StackReference(f"infrastructure.aws.network.{stack_info.name}")
mitodl_zone_id = dns_stack.require_output("odl_zone_id")
data_vpc = network_stack.require_output("data_vpc")
operations_vpc = network_stack.require_output("operations_vpc")
athena_warehouse = data_warehouse_stack.require_output("athena_data_warehouse")
dagster_environment = f"data-{stack_info.env_suffix}"
aws_config = AWSBase(
    tags={"OU": "data", "Environment": dagster_environment},
)
consul_provider = get_consul_provider(stack_info)

dagster_bucket_name = f"dagster-{dagster_environment}"
dagster_s3_permissions: List[Dict[str, Union[str, List[str]]]] = [  # noqa: WPS234
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
]

athena_permissions: List[Dict[str, Union[str, List[str]]]] = [  # noqa: WPS234
    {
        "Effect": "Allow",
        "Action": [
            "athena:ListDataCatalogs",
            "athena:ListWorkGroups",
        ],
        "Resource": ["*"],
    },
    {
        "Effect": "Allow",
        "Action": [
            "athena:ListTagsForResource",
            "athena:TagResource",
            "athena:UntagResource",
        ],
        "Resource": [
            f"arn:*:athena:*:*:workgroup/*{stack_info.env_suffix}",
            "arn:*:athena:*:*:datacatalog/*",
        ],
    },
    {
        "Effect": "Allow",
        "Action": [
            "athena:BatchGetNamedQuery",
            "athena:BatchGetQueryExecution",
            "athena:CreateNamedQuery",
            "athena:DeleteNamedQuery",
            "athena:GetNamedQuery",
            "athena:GetQueryExecution",
            "athena:GetQueryResults",
            "athena:GetQueryResultsStream",
            "athena:GetWorkGroup",
            "athena:ListNamedQueries",
            "athena:ListQueryExecutions",
            "athena:StartQueryExecution",
            "athena:StopQueryExecution",
            "athena:UpdateWorkGroup",
        ],
        "Resource": [f"arn:*:athena:*:*:workgroup/*{stack_info.env_suffix}"],
    },
    {
        "Effect": "Allow",
        "Action": [
            "athena:CreateDataCatalog",
            "athena:DeleteDataCatalog",
            "athena:GetDataCatalog",
            "athena:GetDatabase",
            "athena:GetTableMetadata",
            "athena:ListDatabases",
            "athena:ListTableMetadata",
            "athena:UpdateDataCatalog",
        ],
        "Resource": ["arn:*:athena:*:*:datacatalog/*"],
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
            f"arn:aws:glue:*:*:database/*{stack_info.env_suffix}",
            f"arn:aws:glue:*:*:table/*{stack_info.env_suffix}/*",
        ],
    },
]

dagster_iam_permissions = {
    "Version": "2012-10-17",
    "Statement": dagster_s3_permissions + athena_permissions,
}

parliament_config = {"RESOURCE_EFFECTIVELY_STAR": {"ignore_locations": []}}

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

dagster_profile = iam.InstanceProfile(
    f"dagster-instance-profile-{stack_info.env_suffix}",
    role=dagster_role.name,
    name=f"etl-instance-profile-{stack_info.env_suffix}",
    path="/ol-data/etl-profile/",
)

dagster_instance_security_group = ec2.SecurityGroup(
    f"dagster-instance-security-group-{stack_info.env_suffix}",
    name=f"dagster-instance-{dagster_environment}",
    description="Access control to and from the Dagster instance",
    tags=aws_config.tags,
    vpc_id=data_vpc["id"],
)

dagster_db_security_group = ec2.SecurityGroup(
    f"dagster-db-access-{stack_info.env_suffix}",
    name=f"ol-etl-db-access-{stack_info.env_suffix}",
    description="Access from the data VPC to the Dagster database",
    ingress=[
        ec2.SecurityGroupIngressArgs(
            cidr_blocks=[data_vpc["cidr"], operations_vpc["cidr"]],
            ipv6_cidr_blocks=[data_vpc["cidr_v6"]],
            protocol="tcp",
            from_port=5432,  # noqa: WPS432
            to_port=5432,  # noqa: WPS432
        )
    ],
    tags=aws_config.tags,
    vpc_id=data_vpc["id"],
)

dagster_db_config = OLPostgresDBConfig(
    instance_name=f"ol-etl-db-{stack_info.env_suffix}",
    password=get_config("dagster:db_password"),
    subnet_group_name=data_vpc["rds_subnet"],
    security_groups=[dagster_db_security_group],
    tags=aws_config.tags,
    db_name="dagster",
    **defaults(stack_info)["rds"],
)
dagster_db = OLAmazonDB(dagster_db_config)

dagster_db_vault_backend_config = OLVaultPostgresDatabaseConfig(
    db_name=dagster_db_config.db_name,
    mount_point=f"{dagster_db_config.engine}-dagster-{dagster_environment}",
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

dagster_minion_id = f"dagster-{dagster_environment}-0"
salt_minion = OLSaltStackMinion(
    f"saltstack-minion-{dagster_minion_id}",
    OLSaltStackMinionInputs(
        minion_id=dagster_minion_id,
        salt_api_url=get_config("saltstack:api_url"),
        salt_user=get_config("saltstack:api_user"),
        salt_password=get_config("saltstack:api_password"),
    ),
)

cloud_init_userdata = build_userdata(
    instance_name=dagster_minion_id,
    minion_keys=salt_minion,
    minion_roles=["dagster"],
    minion_environment=dagster_environment,
    salt_host=f"salt-{stack_info.env_suffix}.private.odl.mit.edu",
)

dagster_image = ec2.get_ami(
    filters=[
        {"name": "virtualization-type", "values": ["hvm"]},
        {"name": "root-device-type", "values": ["ebs"]},
        {"name": "name", "values": ["debian-11-amd64*"]},
        {"name": "image-id", "values": ["ami-00fd4f335e00c21ce"]},
    ],
    most_recent=True,
    owners=["136693071363"],
)
instance_tags = aws_config.merged_tags({"Name": dagster_minion_id})

dagster_instance = ec2.Instance(
    f"dagster-instance-{dagster_environment}",
    ami=dagster_image.id,
    user_data=cloud_init_userdata,
    instance_type=get_config("dagster:instance_type"),
    iam_instance_profile=dagster_profile.id,
    tags=instance_tags,
    volume_tags=instance_tags,
    subnet_id=data_vpc["subnet_ids"][0],
    key_name=get_config("saltstack:key_name"),
    root_block_device=ec2.InstanceRootBlockDeviceArgs(
        volume_type=DiskTypes.ssd, volume_size=100
    ),
    vpc_security_group_ids=[
        data_vpc["security_groups"]["default"],
        data_vpc["security_groups"]["web"],
        data_vpc["security_groups"]["salt_minion"],
        dagster_instance_security_group.id,
    ],
    opts=ResourceOptions(depends_on=[salt_minion]),
)

dagster_elastic_ip = ec2.Eip(
    "dagster-instance-elastic-ip", instance=dagster_instance.id, vpc=True
)

fifteen_minutes = 60 * 15
dagster_domain = route53.Record(
    f"dagster-{stack_info.env_suffix}-service-domain",
    name=get_config("dagster:domain"),
    type="A",
    ttl=fifteen_minutes,
    records=[dagster_instance.public_ip],
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
        "security_group": dagster_instance_security_group.id,
    },
)
