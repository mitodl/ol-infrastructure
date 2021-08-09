"""Create the resources needed to run a Concourse CI/CD system.

- Launch a PostgreSQL instance in RDS
- Create the IAM policies needed to grant the needed access for various build pipelines
- Create an autoscaling group for Concourse web nodes
- Create an autoscaling group for Concourse worker instances
"""
import base64
import json
from pathlib import Path

import pulumi_vault as vault
import yaml
from pulumi import Config, StackReference
from pulumi_aws import acm, autoscaling, ec2, get_caller_identity, iam, lb, route53
from pulumi_consul import Node, Service, ServiceCheckArgs

from bridge.lib.magic_numbers import (
    DEFAULT_HTTPS_PORT,
    DEFAULT_POSTGRES_PORT,
    MAXIMUM_PORT_NUMBER,
)
from ol_infrastructure.components.aws.database import OLAmazonDB, OLPostgresDBConfig
from ol_infrastructure.components.services.vault import (
    OLVaultDatabaseBackend,
    OLVaultPostgresDatabaseConfig,
)
from ol_infrastructure.lib.aws.ec2_helper import (
    DiskTypes,
    InstanceTypes,
    default_egress_args,
)
from ol_infrastructure.lib.aws.iam_helper import IAM_POLICY_VERSION, lint_iam_policy
from ol_infrastructure.lib.ol_types import AWSBase
from ol_infrastructure.lib.pulumi_helper import parse_stack
from ol_infrastructure.lib.stack_defaults import defaults

concourse_config = Config("concourse")
stack_info = parse_stack()
network_stack = StackReference(f"infrastructure.aws.network.{stack_info.name}")
policy_stack = StackReference("infrastructure.aws.policies")
dns_stack = StackReference("infrastructure.aws.dns")
consul_stack = StackReference(f"infrastructure.consul.operations.{stack_info.name}")
mitodl_zone_id = dns_stack.require_output("odl_zone_id")

consul_security_groups = consul_stack.require_output("security_groups")
operations_vpc = network_stack.require_output("operations_vpc")
aws_config = AWSBase(
    tags={"OU": "operations", "Environment": f"operations-{stack_info.env_suffix}"}
)
aws_account = get_caller_identity()
ops_vpc_id = operations_vpc["id"]
concourse_web_ami = ec2.get_ami(
    filters=[
        ec2.GetAmiFilterArgs(name="name", values=["concourse-web-*"]),
        ec2.GetAmiFilterArgs(name="virtualization-type", values=["hvm"]),
        ec2.GetAmiFilterArgs(name="root-device-type", values=["ebs"]),
    ],
    most_recent=True,
    owners=[aws_account.account_id],
)

concourse_worker_ami = ec2.get_ami(
    filters=[
        ec2.GetAmiFilterArgs(name="name", values=["concourse-worker-*"]),
        ec2.GetAmiFilterArgs(name="virtualization-type", values=["hvm"]),
        ec2.GetAmiFilterArgs(name="root-device-type", values=["ebs"]),
    ],
    most_recent=True,
    owners=[aws_account.account_id],
)
concourse_web_tag = f"concourse-web-{stack_info.env_suffix}"

###################################
#    Security & Access Control    #
###################################

# AWS Permissions Document
# S3 bucket permissions for publishing OCW
# S3 bucket permissions for uploading software artifacts
concourse_iam_permissions = {
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
                "s3:DeleteObject",
                "s3:ListBucket*",
            ],
            "Resource": [
                "arn:aws:s3:::*-edxapp-mfe",
                "arn:aws:s3:::*-edxapp-mfe/*",
                "arn:aws:s3:::ocw-content*",
                "arn:aws:s3:::ocw-content*/*",
                "arn:aws:s3:::ol-eng-artifacts",
                "arn:aws:s3:::ol-eng-artifacts/*",
            ],
        },
        {
            "Effect": "Allow",
            "Action": ["s3:GetObject*", "s3:ListBucket"],
            "Resource": [
                "arn:aws:s3:::ol-ocw-studio-app*",
                "arn:aws:s3:::ol-ocw-studio-app*/*",
            ],
        },
    ],
}

concourse_iam_policy = iam.Policy(
    "cicd-iam-permissions-policy",
    path=f"/ol-infrastructure/iam/cicd-{stack_info.env_suffix}/",
    policy=lint_iam_policy(concourse_iam_permissions, stringify=True),
    name_prefix=f"cicd-policy-{stack_info.env_suffix}",
    tags=aws_config.tags,
)

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

iam.RolePolicyAttachment(
    f"concourse-route53-role-policy-{stack_info.env_suffix}",
    policy_arn=policy_stack.require_output("iam_policies")["route53_odl_zone_records"],
    role=concourse_instance_role.name,
)

iam.RolePolicyAttachment(
    "concourse-cicd-permissions-policy",
    policy_arn=concourse_iam_policy.arn,
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
    type="kv-v2",
)
vault.generic.Secret(
    "concourse-web-secret-values",
    path=concourse_secrets_mount.path.apply(lambda mount_path: f"{mount_path}/web"),
    data_json=concourse_config.require_secret_object("web_vault_secrets").apply(
        json.dumps
    ),
)
vault.generic.Secret(
    "concourse-worker-secret-values",
    path=concourse_secrets_mount.path.apply(lambda mount_path: f"{mount_path}/worker"),
    data_json=concourse_config.require_secret_object("worker_vault_secrets").apply(
        json.dumps
    ),
)
vault.generic.Secret(
    "concourse-dockerhub-credentials",
    path=concourse_secrets_mount.path.apply(
        lambda mount_path: f"{mount_path}/main/dockerhub"
    ),
    data_json=concourse_config.require_secret_object("dockerhub_credentials").apply(
        json.dumps
    ),
)
vault.generic.Secret(
    "concourse-pypi-credentials",
    path=concourse_secrets_mount.path.apply(
        lambda mount_path: f"{mount_path}/main/pypi_creds"
    ),
    data_json=concourse_config.require_secret_object("pypi_credentials").apply(
        json.dumps
    ),
)
vault.generic.Secret(
    "concourse-consul-credentials",
    path=concourse_secrets_mount.path.apply(
        lambda mount_path: f"{mount_path}/main/consul"
    ),
    data_json=concourse_config.require_secret_object("consul_credentials").apply(
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
    auth_type="iam",
    role="concourse-web",
    inferred_entity_type="ec2_instance",
    inferred_aws_region="us-east-1",
    bound_iam_instance_profile_arns=[concourse_instance_profile.arn],
    bound_ami_ids=[concourse_web_ami.id],
    bound_account_ids=[aws_account.account_id],
    bound_vpc_ids=[ops_vpc_id],
    token_policies=[concourse_vault_policy.name],
)

vault.aws.AuthBackendRole(
    "concourse-worker-ami-ec2-vault-auth",
    backend="aws",
    auth_type="iam",
    inferred_entity_type="ec2_instance",
    inferred_aws_region="us-east-1",
    bound_iam_instance_profile_arns=[concourse_instance_profile.arn],
    role="concourse-worker",
    bound_ami_ids=[concourse_worker_ami.id],
    bound_account_ids=[aws_account.account_id],
    bound_vpc_ids=[ops_vpc_id],
    token_policies=[concourse_vault_policy.name],
)

##################################
#     Network Access Control     #
##################################

# Create worker node security group
concourse_worker_security_group = ec2.SecurityGroup(
    f"concourse-worker-security-group-{stack_info.env_suffix}",
    name=f"concourse-worker-operations-{stack_info.env_suffix}",
    description="Access control for Concourse worker servers",
    egress=default_egress_args,
    vpc_id=ops_vpc_id,
)

# Create web node security group
concourse_web_security_group = ec2.SecurityGroup(
    f"concourse-web-security-group-{stack_info.env_suffix}",
    name=f"concourse-web-operations-{stack_info.env_suffix}",
    description="Access control for Concourse web servers",
    ingress=[
        ec2.SecurityGroupIngressArgs(
            self=True,
            security_groups=[concourse_worker_security_group.id],
            from_port=0,
            to_port=MAXIMUM_PORT_NUMBER,
            protocol="tcp",
            description="Allow Concourse workers to connect to Concourse web nodes",
        )
    ],
    egress=default_egress_args,
    vpc_id=ops_vpc_id,
)

ec2.SecurityGroupRule(
    "concourse-worker-access-from-concourse-web",
    security_group_id=concourse_worker_security_group.id,
    source_security_group_id=concourse_web_security_group.id,
    protocol="tcp",
    from_port=0,
    to_port=MAXIMUM_PORT_NUMBER,
    description="Allow all traffic from Concourse web nodes to workers",
    type="ingress",
)

# Create security group for Concourse Postgres database
concourse_db_security_group = ec2.SecurityGroup(
    f"concourse-db-access-{stack_info.env_suffix}",
    name=f"concourse-db-access-{stack_info.env_suffix}",
    description="Access from Concourse instances to the associated Postgres database",
    ingress=[
        ec2.SecurityGroupIngressArgs(
            security_groups=[concourse_web_security_group.id],
            # TODO: Create Vault security group to act as source of allowed
            # traffic. (TMM 2021-05-04)
            cidr_blocks=[operations_vpc["cidr"]],
            protocol="tcp",
            from_port=DEFAULT_POSTGRES_PORT,
            to_port=DEFAULT_POSTGRES_PORT,
            description="Access to Postgres from Concourse web nodes",
        ),
    ],
    tags=aws_config.tags,
    vpc_id=ops_vpc_id,
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
    engine_version="12.5",
    **defaults(stack_info)["rds"],
)
concourse_db = OLAmazonDB(concourse_db_config)

concourse_db_vault_backend_config = OLVaultPostgresDatabaseConfig(
    db_name=concourse_db_config.db_name,
    mount_point=f"{concourse_db_config.engine}-concourse",
    db_admin_username=concourse_db_config.username,
    db_admin_password=concourse_config.require("db_password"),
    db_host=concourse_db.db_instance.address,
)
concourse_db_vault_backend = OLVaultDatabaseBackend(concourse_db_vault_backend_config)

if stack_info.env_suffix == "production":
    consul_datacenter = "operations"
else:
    consul_datacenter = "operations-qa"
concourse_db_consul_node = Node(
    "concourse-instance-db-node",
    name="concourse-postgres-db",
    address=concourse_db.db_instance.address,
    datacenter=consul_datacenter,
)

concourse_db_consul_service = Service(
    "concourse-instance-db-service",
    node=concourse_db_consul_node.name,
    name="concourse-postgres",
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
    name=concourse_web_tag,
    ip_address_type="dualstack",
    load_balancer_type="application",
    enable_http2=True,
    subnets=operations_vpc["subnet_ids"],
    security_groups=[
        operations_vpc["security_groups"]["web"],
    ],
    tags=aws_config.merged_tags({"Name": concourse_web_tag}),
)

TARGET_GROUP_NAME_MAX_LENGTH = 32
web_lb_target_group = lb.TargetGroup(
    "concourse-web-alb-target-group",
    vpc_id=ops_vpc_id,
    target_type="instance",
    port=DEFAULT_HTTPS_PORT,
    protocol="HTTPS",
    health_check=lb.TargetGroupHealthCheckArgs(
        healthy_threshold=2,
        interval=10,
        path="/api/v1/info",
        port=str(DEFAULT_HTTPS_PORT),
        protocol="HTTPS",
    ),
    name=concourse_web_tag[:TARGET_GROUP_NAME_MAX_LENGTH],
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
    protocol="HTTPS",
    default_actions=[
        lb.ListenerDefaultActionArgs(
            type="forward",
            target_group_arn=web_lb_target_group.arn,
        )
    ],
)

# Create auto scale group and launch configs for Concourse web and worker
web_instance_type = (
    concourse_config.get("web_instance_type") or InstanceTypes.medium.name
)
web_launch_config = ec2.LaunchTemplate(
    "concourse-web-launch-template",
    name_prefix=f"concourse-web-{stack_info.env_suffix}-",
    description="Launch template for deploying Concourse web nodes",
    iam_instance_profile=ec2.LaunchTemplateIamInstanceProfileArgs(
        arn=concourse_instance_profile.arn,
    ),
    image_id=concourse_web_ami.id,
    vpc_security_group_ids=[
        concourse_web_security_group.id,
        operations_vpc["security_groups"]["web"],
        consul_security_groups["consul_agent"],
    ],
    instance_type=InstanceTypes[web_instance_type].value,
    key_name="oldevops",
    tag_specifications=[
        ec2.LaunchTemplateTagSpecificationArgs(
            resource_type="instance",
            tags=aws_config.merged_tags({"Name": concourse_web_tag}),
        ),
        ec2.LaunchTemplateTagSpecificationArgs(
            resource_type="volume",
            tags=aws_config.merged_tags({"Name": concourse_web_tag}),
        ),
    ],
    tags=aws_config.tags,
    user_data=base64.b64encode(
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
                                        f"tag_value=operations-{stack_info.env_suffix}"
                                    ],
                                    "datacenter": consul_datacenter,
                                }
                            ),
                            "owner": "consul:consul",
                        },
                        {
                            "path": "/etc/default/caddy",
                            "content": "DOMAIN={}".format(
                                concourse_config.require("web_host_domain")
                            ),
                        },
                    ]
                },
                sort_keys=True,
            )
        ).encode("utf8")
    ).decode("utf8"),
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
        preferences=autoscaling.GroupInstanceRefreshPreferencesArgs(
            min_healthy_percentage=50  # noqa: WPS432
        ),
        triggers=["tags"],
    ),
    target_group_arns=[web_lb_target_group.arn],
    tags=[
        autoscaling.GroupTagArgs(
            key=key_name,
            value=key_value,
            propagate_at_launch=True,
        )
        for key_name, key_value in aws_config.merged_tags(
            {"ami_id": concourse_web_ami.id}
        ).items()
    ],
)

worker_instance_type = (
    concourse_config.get("worker_instance_type") or InstanceTypes.large.name
)
worker_launch_config = ec2.LaunchTemplate(
    "concourse-worker-launch-template",
    name_prefix=f"concourse-worker-{stack_info.env_suffix}-",
    description="Launch template for deploying Concourse worker nodes",
    iam_instance_profile=ec2.LaunchTemplateIamInstanceProfileArgs(
        arn=concourse_instance_profile.arn,
    ),
    image_id=concourse_worker_ami.id,
    block_device_mappings=[
        ec2.LaunchTemplateBlockDeviceMappingArgs(
            device_name="/dev/xvda",
            ebs=ec2.LaunchTemplateBlockDeviceMappingEbsArgs(
                volume_size=concourse_config.get_int("worker_disk_size")
                or 25,  # noqa: WPS432
                volume_type=DiskTypes.ssd,
                delete_on_termination=True,
            ),
        )
    ],
    vpc_security_group_ids=[
        concourse_worker_security_group.id,
        consul_security_groups["consul_agent"],
    ],
    instance_type=InstanceTypes[worker_instance_type].value,
    key_name="oldevops",
    tag_specifications=[
        ec2.LaunchTemplateTagSpecificationArgs(
            resource_type="instance",
            tags=aws_config.merged_tags(
                {"Name": f"concourse-worker-{stack_info.env_suffix}"}
            ),
        ),
        ec2.LaunchTemplateTagSpecificationArgs(
            resource_type="volume",
            tags=aws_config.merged_tags(
                {"Name": f"concourse-worker-{stack_info.env_suffix}"}
            ),
        ),
    ],
    tags=aws_config.tags,
    user_data=base64.b64encode(
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
                                        f"tag_value=operations-{stack_info.env_suffix}"
                                    ],
                                    "datacenter": consul_datacenter,
                                }
                            ),
                            "owner": "consul:consul",
                        },
                    ]
                },
                sort_keys=True,
            )
        ).encode("utf8")
    ).decode("utf8"),
)
worker_asg = autoscaling.Group(
    "concourse-worker-autoscaling-group",
    desired_capacity=concourse_config.get_int("worker_node_capacity") or 1,
    min_size=1,
    max_size=50,  # noqa: WPS432
    health_check_type="EC2",
    vpc_zone_identifiers=operations_vpc["subnet_ids"],
    launch_template=autoscaling.GroupLaunchTemplateArgs(
        id=worker_launch_config.id, version="$Latest"
    ),
    instance_refresh=autoscaling.GroupInstanceRefreshArgs(
        strategy="Rolling",
        preferences=autoscaling.GroupInstanceRefreshPreferencesArgs(
            min_healthy_percentage=50  # noqa: WPS432
        ),
        triggers=["tags"],
    ),
    tags=[
        autoscaling.GroupTagArgs(
            key=key_name,
            value=key_value,
            propagate_at_launch=True,
        )
        for key_name, key_value in aws_config.merged_tags(
            {"ami_id": concourse_worker_ami.id}
        ).items()
    ],
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
