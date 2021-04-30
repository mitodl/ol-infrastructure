"""Create the resources needed to run a Concourse CI/CD system.

- Launch a PostgreSQL instance in RDS
- Create the IAM policies needed to grant the needed access for various build pipelines
- Create an autoscaling group for Concourse web nodes
- Create an autoscaling group for Concourse worker instances
"""
import json
from pathlib import Path

import pulumi_vault as vault
from pulumi import Config, StackReference
from pulumi_aws import ec2, iam
from pulumi_consul import Node, Service, ServiceCheckArgs

from bridge.lib.magic_numbers import (
    CONCOURSE_WEB_HOST_COMMUNICATION_PORT,
    DEFAULT_POSTGRES_PORT,
)
from ol_infrastructure.components.aws.database import OLAmazonDB, OLPostgresDBConfig
from ol_infrastructure.components.services.vault import (
    OLVaultDatabaseBackend,
    OLVaultPostgresDatabaseConfig,
)
from ol_infrastructure.lib.aws.ec2_helper import default_egress_args
from ol_infrastructure.lib.aws.iam_helper import IAM_POLICY_VERSION
from ol_infrastructure.lib.ol_types import AWSBase
from ol_infrastructure.lib.pulumi_helper import parse_stack
from ol_infrastructure.lib.stack_defaults import defaults

concourse_config = Config("concourse")
stack_info = parse_stack()
network_stack = StackReference(f"infrastructure.aws.network.{stack_info.name}")
policy_stack = StackReference("infrastructure.aws.policies")
dns_stack = StackReference("infrastructure.aws.dns")
mitodl_zone_id = dns_stack.require_output("odl_zone_id")

operations_vpc = network_stack.require_output("operations_vpc")
aws_config = AWSBase(
    tags={"OU": "operations", "Environment": "operations-{stack_info.env_suffix}"}
)

###################################
#    Security & Access Control    #
###################################

# AWS Permissions Document
concourse_iam_permissions = {"Version": IAM_POLICY_VERSION, "Statement": {}}
# KMS key access for decrypting secrets from SOPS and Pulumi
# S3 bucket permissions for publishing OCW
# S3 bucket permissions for uploading software artifacts


# IAM and instance profile
concourse_instance_role = iam.Role(
    f"concourse-instance-role-{stack_info.env_suffix}",
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
    path="/ol-applications/concourse/role/",
    tags=aws_config.tags,
)

iam.RolePolicyAttachment(
    f"concourse-describe-instance-role-policy-{stack_info.env_suffix}",
    policy_arn=policy_stack.require_output("iam_policies")["describe_instances"],
    role=concourse_instance_role.name,
)

concourse_instance_profile = iam.InstanceProfile(
    f"concourse-instance-profile-{stack_info.env_suffix}",
    role=concourse_instance_role.name,
    path="/ol-applications/concourse/profile/",
)

# Mount Vault secrets backend and populate secrets
concourse_secrets_mount = vault.Mount(
    "concourse-app-secrets",
    description="Generic secrets storage for Concourse application deployment",
    path="secret-concourse",
    type="generic",
    options={"version": "2"},
)
vault.generic.Secret(
    "concourse-web-secret-values",
    path=concourse_secrets_mount.path.concat("/web"),
    data_json=concourse_config.require_secret_object("web_vault_secrets").apply(
        json.dumps
    ),
)
vault.generic.Secret(
    "concourse-worker-secret-values",
    path=concourse_secrets_mount.path.concat("/worker"),
    data_json=concourse_config.require_secret_object("worker_vault_secrets").apply(
        json.dumps
    ),
)

# Vault policy definition
vault.Policy(
    "concourse-vault-policy",
    name="concourse",
    policy=Path(__file__).parent.joinpath("concourse_policy.hcl"),
)
# Register Concourse AMI for Vault AWS auth

##################################
#     Network Access Control     #
##################################

# Create worker node security group
concourse_worker_security_group = ec2.SecurityGroup(
    f"concourse-worker-security-group-{stack_info.env_suffix}",
    name="concourse-web-operations-{stack_info.env_suffix}",
    description="Access control for Concourse web servers",
    egress=default_egress_args,
)

# Create web node security group
concourse_web_security_group = ec2.SecurityGroup(
    f"concourse-web-security-group-{stack_info.env_suffix}",
    name="concourse-web-operations-{stack_info.env_suffix}",
    description="Access control for Concourse web servers",
    ingress=[
        ec2.SecurityGroupIngressArgs(
            security_groups=[concourse_worker_security_group.id],
            from_port=CONCOURSE_WEB_HOST_COMMUNICATION_PORT,
            to_port=CONCOURSE_WEB_HOST_COMMUNICATION_PORT,
            protocol="tcp",
            description="Allow Concourse workers to connect to Concourse web nodes",
        )
    ],
    egress=default_egress_args,
)

# Create security group for Concourse Postgres database
concourse_db_security_group = ec2.SecurityGroup(
    f"concourse-db-access-{stack_info.env_suffix}",
    name=f"concourse-db-access-{stack_info.env_suffix}",
    description="Access from Concourse instances to the associated Postgres database",
    ingress=[
        ec2.SecurityGroupIngressArgs(
            security_groups=[concourse_web_security_group.id],
            protocol="tcp",
            from_port=DEFAULT_POSTGRES_PORT,
            to_port=DEFAULT_POSTGRES_PORT,
            description="Access to Postgres from Concourse web nodes",
        )
    ],
    tags=aws_config.tags,
    vpc_id=operations_vpc["id"],
)


##########################
#     Database Setup     #
##########################
concourse_db_config = OLPostgresDBConfig(
    instance_name=f"concourse-db-{stack_info.env_suffix}",
    password=concourse_config.require("db_password"),
    subnet_group_name=operations_vpc["rds_subnet"],
    security_groups=[concourse_db_security_group],
    tags=aws_config.tags,
    db_name="concourse",
    **defaults(stack_info)["rds"],
)
concourse_db = OLAmazonDB(concourse_db_config)

concourse_db_vault_backend_config = OLVaultPostgresDatabaseConfig(
    db_name=concourse_db_config.db_name,
    mount_point=f"{concourse_db_config.engine}-concourse-{stack_info.env_suffix}",
    db_admin_username=concourse_db_config.username,
    db_admin_password=concourse_config.require("db_password"),
    db_host=concourse_db.db_instance.address,
)
concourse_db_vault_backend = OLVaultDatabaseBackend(concourse_db_vault_backend_config)

concourse_db_consul_node = Node(
    "concourse-instance-db-node",
    name="concourse-postgres-db",
    address=concourse_db.db_instance.address,
    datacenter=f"operations-{stack_info.env_suffix}",
)

concourse_db_consul_service = Service(
    "concourse-instance-db-service",
    node=concourse_db_consul_node.name,
    name="concourse-db",
    port=concourse_db_config.port,
    meta={
        "external-node": True,
        "external-probe": True,
    },
    checks=[
        ServiceCheckArgs(
            check_id="concourse-instance-db",
            interval="10s",
            name="concourse-instance-id",
            timeout="60s",
            status="passing",
            tcp=f"{concourse_db.db_instance.address}:{concourse_db_config.port}",  # noqa: WPS237,E501
        )
    ],
)

##########################
#     EC2 Deployment     #
##########################

# Create Vault role definitions for web and worker EC2 auth backend

# Create auto scale group and launch configs for Concourse web and worker

# Create userdata for Consul configuration to join correct cluster
# Create userdata for setting Caddy domain

# Create load balancer for Concourse web nodes

# Create Route53 DNS records for Concourse web nodes


# Create RDS postgres instance

# Register Postgres instance in Vault
