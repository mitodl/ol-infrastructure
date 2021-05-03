"""Create the resources needed to run a Concourse CI/CD system.

- Launch a PostgreSQL instance in RDS
- Create the IAM policies needed to grant the needed access for various build pipelines
- Create an autoscaling group for Concourse web nodes
- Create an autoscaling group for Concourse worker instances
"""
import json
from pathlib import Path

import pulumi_vault as vault
import yaml
from pulumi import Config, StackReference
from pulumi_aws import acm, autoscaling, ec2, get_caller_identity, iam, lb, route53
from pulumi_consul import Node, Service, ServiceCheckArgs

from bridge.lib.magic_numbers import (
    CONCOURSE_WEB_HOST_COMMUNICATION_PORT,
    DEFAULT_HTTPS_PORT,
    DEFAULT_POSTGRES_PORT,
)
from ol_infrastructure.components.aws.database import OLAmazonDB, OLPostgresDBConfig
from ol_infrastructure.components.services.vault import (
    OLVaultDatabaseBackend,
    OLVaultPostgresDatabaseConfig,
)
from ol_infrastructure.lib.aws.ec2_helper import InstanceTypes, default_egress_args
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
aws_account = get_caller_identity()
concourse_web_ami = ec2.get_ami(
    filters=[
        ec2.GetAmiFilterArgs(name="name", values=["concourse-web"]),
        ec2.GetAmiFilterArgs(name="virtualization-type", values=["hvm"]),
        ec2.GetAmiFilterArgs(name="root-device-type", values=["ebs"]),
    ],
    most_recent=True,
    owners=[aws_account.account_id],
)

concourse_worker_ami = ec2.get_ami(
    filters=[
        ec2.GetAmiFilterArgs(name="name", values=["concourse-worker"]),
        ec2.GetAmiFilterArgs(name="virtualization-type", values=["hvm"]),
        ec2.GetAmiFilterArgs(name="root-device-type", values=["ebs"]),
    ],
    most_recent=True,
    owners=[aws_account.account_id],
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
concourse_vault_policy = vault.Policy(
    "concourse-vault-policy",
    name="concourse",
    policy=Path(__file__).parent.joinpath("concourse_policy.hcl").read_text(),
)
# Register Concourse AMI for Vault AWS auth
vault.aws.AuthBackendRole(
    "concourse-web-ami-ec2-vault-auth",
    backend="aws",
    auth_type="ec2",
    role="concourse-web",
    bound_ami_ids=[concourse_web_ami.id],
    bound_account_ids=[aws_account.account_id],
    bound_vpc_ids=[operations_vpc["id"]],
    token_policies=[concourse_vault_policy.name],
)

vault.aws.AuthBackendRole(
    "concourse-worker-ami-ec2-vault-auth",
    backend="aws",
    auth_type="ec2",
    role="concourse-worker",
    bound_ami_ids=[concourse_worker_ami.id],
    bound_account_ids=[aws_account.account_id],
    bound_vpc_ids=[operations_vpc["id"]],
    token_policies=[concourse_vault_policy.name],
)

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

# Create load balancer for Concourse web nodes
web_lb = lb.LoadBalancer(
    "concourse-web-load-balancer",
    name=f"concourse-web-{stack_info.env_suffix}",
    ip_address_type="dualstack",
    load_balancer_type="application",
    enable_http2=True,
    subnets=operations_vpc["subnet_ids"],
    security_groups=[
        operations_vpc["security_groups"]["web"],
    ],
    tags=aws_config.merged_tags({"Name": f"concourse-web-{stack_info.env_suffix}"}),
)

web_lb_target_group = lb.TargetGroup(
    "concourse-web-alb-target-group",
    vpc_id=operations_vpc["id"],
    target_type="ip",
    port=DEFAULT_HTTPS_PORT,
    protocol="HTTPS",
    health_check=lb.TargetGroupHealthCheckArgs(
        healthy_threshold=2,
        interval=10,
        path="/",
        port=str(DEFAULT_HTTPS_PORT),
        protocol="HTTPS",
    ),
    name=f"concourse-web-alb-group-{stack_info.env_suffix}",
    tags=aws_config.tags,
)
concourse_web_acm_cert = acm.get_certificate(
    domain="*.odl.mit.edu", most_recent=True, statuses=["ISSUED"]
)
concourse_web_alb_listener = lb.Listener(
    "concourse-web-alb-listener",
    certificate_arn=concourse_web_acm_cert.arn,
    load_balancer_arn=web_lb.arn,
    port=DEFAULT_HTTPS_PORT,
    default_actions=[
        lb.ListenerDefaultActionArgs(
            type="forward",
            target_group_arn=web_lb_target_group.arn,
        )
    ],
)

# Create auto scale group and launch configs for Concourse web and worker
web_launch_config = ec2.LaunchTemplate(
    "concourse-web-launch-template",
    name_prefix=f"concourse-web-{stack_info.env_suffix}-",
    description="Launch template for deploying Concourse web nodes",
    iam_instance_profile=ec2.LaunchTemplateIamInstanceProfileArgs(
        arn=concourse_instance_profile.arn,
        name=concourse_instance_profile.name,
    ),
    image_id=concourse_web_ami.id,
    vpc_security_group_ids=[
        concourse_web_security_group.id,
        operations_vpc["security_groups"]["web"],
    ],
    instance_type=InstanceTypes.medium,
    key_name="salt-production",
    tag_specifications=[
        ec2.LaunchTemplateTagSpecificationArgs(
            resource_type="instance",
            tags=aws_config.tags,
        ),
        ec2.LaunchTemplateTagSpecificationArgs(
            resource_type="volume",
            tags=aws_config.tags,
        ),
    ],
    tags=aws_config.tags,
    user_data="#cloud-config\n{}".format(
        yaml.dump(
            {
                "write_files": [
                    {
                        "path": "/etc/consul.d/02-autojoin.json",
                        "contents": json.dumps(
                            {
                                "retry_join": [
                                    "provider=aws tag_key=consul_env "
                                    f"tag_value=operations-{stack_info.env_suffix}"
                                ]
                            }
                        ),
                    },
                    {
                        "path": "/etc/default/caddy",
                        "contents": "DOMAIN={}".format(
                            concourse_config.require("web_host_domain")
                        ),
                    },
                ]
            },
            sort_keys=True,
        )
    ),
)
web_asg = autoscaling.Group(
    "concourse-web-autoscaling-group",
    desired_capacity=concourse_config.get_int("web_node_capacity") or 1,
    min_size=1,
    max_size=5,
    health_check_type="ELB",
    vpc_zone_identifiers=operations_vpc["subnet_ids"],
    launch_template=autoscaling.GroupLaunchTemplateArgs(
        id=web_launch_config.id, version="$Latest"
    ),
    instance_refresh=autoscaling.GroupInstanceRefreshArgs(
        strategy="Rolling",
    ),
    target_group_arns=[web_lb_target_group.arn],
)

worker_launch_config = ec2.LaunchTemplate(
    "concourse-worker-launch-template",
    name_prefix=f"concourse-worker-{stack_info.env_suffix}-",
    description="Launch template for deploying Concourse worker nodes",
    iam_instance_profile=ec2.LaunchTemplateIamInstanceProfileArgs(
        arn=concourse_instance_profile.arn,
        name=concourse_instance_profile.name,
    ),
    image_id=concourse_worker_ami.id,
    vpc_security_group_ids=[
        concourse_worker_security_group.id,
    ],
    instance_type=InstanceTypes.large,
    key_name="salt-production",
    tag_specifications=[
        ec2.LaunchTemplateTagSpecificationArgs(
            resource_type="instance",
            tags=aws_config.tags,
        ),
        ec2.LaunchTemplateTagSpecificationArgs(
            resource_type="volume",
            tags=aws_config.tags,
        ),
    ],
    tags=aws_config.tags,
    user_data="#cloud-config\n{}".format(
        yaml.dump(
            {
                "write_files": [
                    {
                        "path": "/etc/consul.d/02-autojoin.json",
                        "contents": json.dumps(
                            {
                                "retry_join": [
                                    "provider=aws tag_key=consul_env "
                                    f"tag_value=operations-{stack_info.env_suffix}"
                                ]
                            }
                        ),
                    },
                ]
            },
            sort_keys=True,
        )
    ),
)
worker_asg = autoscaling.Group(
    "concourse-worker-autoscaling-group",
    desired_capacity=concourse_config.get_int("web_node_capacity") or 1,
    min_size=1,
    max_size=50,  # noqa: WPS432
    health_check_type="EC2",
    vpc_zone_identifiers=operations_vpc["subnet_ids"],
    launch_template=autoscaling.GroupLaunchTemplateArgs(
        id=worker_launch_config.id, version="$Latest"
    ),
    instance_refresh=autoscaling.GroupInstanceRefreshArgs(
        strategy="Rolling",
    ),
)

# Create Route53 DNS records for Concourse web nodes
five_minutes = 60 * 5
route53.Record(
    "concourse-web-dns-record",
    name=concourse_config.require("web_host_domain"),
    type="CNAME",
    ttl=five_minutes,
    records=[web_lb.dns_name],
    zone_id=mitodl_zone_id,
)
