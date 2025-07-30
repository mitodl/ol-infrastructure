import base64
import json
from pathlib import Path

import pulumi_vault as vault
import yaml
from pulumi import Config, ResourceOptions, StackReference
from pulumi.config import get_config
from pulumi_aws import ec2, get_caller_identity, iam, route53
from pulumi_consul import Node, Service, ServiceCheckArgs

from bridge.lib.magic_numbers import (
    AWS_RDS_DEFAULT_DATABASE_CAPACITY,
    DEFAULT_POSTGRES_PORT,
)
from ol_infrastructure.components.aws.database import OLAmazonDB, OLPostgresDBConfig
from ol_infrastructure.components.services.vault import (
    OLVaultDatabaseBackend,
    OLVaultPostgresDatabaseConfig,
)
from ol_infrastructure.lib.aws.ec2_helper import DiskTypes, default_egress_args
from ol_infrastructure.lib.aws.rds_helper import DBInstanceTypes
from ol_infrastructure.lib.consul import get_consul_provider
from ol_infrastructure.lib.ol_types import AWSBase
from ol_infrastructure.lib.pulumi_helper import parse_stack
from ol_infrastructure.lib.stack_defaults import defaults
from ol_infrastructure.lib.vault import setup_vault_provider

setup_vault_provider()
stack_info = parse_stack()

semantic_config = Config("semantic")
dns_stack = StackReference("infrastructure.aws.dns")
network_stack = StackReference(f"infrastructure.aws.network.{stack_info.name}")
policy_stack = StackReference("infrastructure.aws.policies")
vault_stack = StackReference(f"infrastructure.vault.operations.{stack_info.name}")
consul_stack = StackReference(f"infrastructure.consul.applications.{stack_info.name}")
mitodl_zone_id = dns_stack.require_output("odl_zone_id")
apps_vpc = network_stack.require_output("applications_vpc")
semantic_environment = f"operations-{stack_info.env_suffix}"
aws_config = AWSBase(
    tags={"OU": "operations", "Environment": semantic_environment},
)
consul_provider = get_consul_provider(stack_info)

consul_security_groups = consul_stack.require_output("security_groups")
aws_account = get_caller_identity()

semantic_role = iam.Role(
    "semantic-poc-instance-role",
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
    name=f"semantic-instance-role-{stack_info.env_suffix}",
    path="/ol-infrastructure/semantic-role/",
    tags=aws_config.tags,
)

iam.RolePolicyAttachment(
    f"semantic-describe-instances-role-policy-{stack_info.env_suffix}",
    policy_arn=policy_stack.require_output("iam_policies")["describe_instances"],
    role=semantic_role.name,
)
iam.RolePolicyAttachment(
    f"semantic-route53-role-policy-{stack_info.env_suffix}",
    policy_arn=policy_stack.require_output("iam_policies")["route53_odl_zone_records"],
    role=semantic_role.name,
)

semantic_profile = iam.InstanceProfile(
    f"semantic-instance-profile-{stack_info.env_suffix}",
    role=semantic_role.name,
    name=f"semantic-poc-instance-profile-{stack_info.env_suffix}",
    path="/ol-infrastructure/semantic-profile/",
)
semantic_server_security_group = ec2.SecurityGroup(
    f"semantic-server-security-group-{stack_info.env_suffix}",
    name=f"semantic-server-security-group-{stack_info.env_suffix}",
    ingress=[
        # ec2.SecurityGroupIngressArgs(
        # ),
        ec2.SecurityGroupIngressArgs(
            protocol="tcp",
            from_port=8011,
            to_port=8011,
            cidr_blocks=["0.0.0.0/0"],
            description="Allow mike and matt to connect to http.",
        ),
    ],
    egress=default_egress_args,
    vpc_id=apps_vpc["id"],
)

semantic_database_security_group = ec2.SecurityGroup(
    f"semantic-database-security-group-{stack_info.env_suffix}",
    name=f"semantic-database-security-group-{stack_info.env_suffix}",
    description="Access control for the semantic database.",
    ingress=[
        ec2.SecurityGroupIngressArgs(
            security_groups=[
                semantic_server_security_group.id,
                consul_security_groups["consul_server"],
                vault_stack.require_output("vault_server")["security_group"],
            ],
            protocol="tcp",
            from_port=DEFAULT_POSTGRES_PORT,
            to_port=DEFAULT_POSTGRES_PORT,
            description="Access to postgres from semantic servers.",
        ),
        ec2.SecurityGroupIngressArgs(
            security_groups=[],
            protocol="tcp",
            from_port=DEFAULT_POSTGRES_PORT,
            to_port=DEFAULT_POSTGRES_PORT,
            cidr_blocks=["73.218.126.92/32", "98.110.175.231/32"],
            description="Allow mike and matt to connect to postgres.",
        ),
    ],
    vpc_id=apps_vpc["id"],
    tags=aws_config.tags,
)

rds_defaults = defaults(stack_info)["rds"]
rds_defaults["instance_size"] = (
    semantic_config.get("db_instance_size") or DBInstanceTypes.small.value
)

semantic_db_config = OLPostgresDBConfig(
    instance_name=f"semantic-db-{stack_info.env_suffix}",
    password=semantic_config.get("db_password"),
    subnet_group_name=apps_vpc["rds_subnet"],
    security_groups=[semantic_database_security_group],
    storage=semantic_config.get("db_capacity")
    or str(AWS_RDS_DEFAULT_DATABASE_CAPACITY),
    engine_major_version="15",
    tags=aws_config.tags,
    db_name="semantic",
    **defaults(stack_info)["rds"],
)
semantic_db = OLAmazonDB(semantic_db_config)

semantic_db_vault_backend_config = OLVaultPostgresDatabaseConfig(
    db_name=semantic_db_config.db_name,
    mount_point=f"{semantic_db_config.engine}-semantic",
    db_admin_username=semantic_db_config.username,
    db_admin_password=semantic_config.get("db_password"),
    db_host=semantic_db.db_instance.address,
)
semantic_db_vault_backend = OLVaultDatabaseBackend(semantic_db_vault_backend_config)

semantic_db_consul_node = Node(
    "semantic-postgres-db",
    name="semantic-postgres-db",
    address=semantic_db.db_instance.address,
    opts=consul_provider,
)

semantic_db_consul_service = Service(
    "semantic-instance-db-service",
    node=semantic_db_consul_node.name,
    name="semantic-db",
    port=semantic_db_config.port,
    meta={
        "external-node": True,
        "external-probe": True,
    },
    checks=[
        ServiceCheckArgs(
            check_id="semantic-instance-db",
            interval="10s",
            name="semantic-instance-id",
            timeout="60s",
            status="passing",
            tcp=semantic_db.db_instance.address.apply(
                lambda address: f"{address}:{semantic_db_config.port}"
            ),
        )
    ],
    opts=consul_provider,
)

# Get the AMI ID for the semantic/docker-compose image
semantic_image = ec2.get_ami(
    filters=[
        ec2.GetAmiFilterArgs(name="name", values=["semantic-*"]),
        ec2.GetAmiFilterArgs(name="virtualization-type", values=["hvm"]),
        ec2.GetAmiFilterArgs(name="root-device-type", values=["ebs"]),
    ],
    most_recent=True,
    owners=[aws_account.account_id],
)

# Create a vault policy to allow dagster to get to the secrets it needs
semantic_server_vault_policy = vault.Policy(
    "semantic-server-vault-policy",
    name="semantic-server",
    policy=Path(__file__).parent.joinpath("semantic_server_policy.hcl").read_text(),
)
# Register semantic AMI for Vault AWS auth
vault.aws.AuthBackendRole(
    "semantic-server-ami-ec2-vault-auth",
    backend="aws",
    auth_type="iam",
    role="semantic-server",
    inferred_entity_type="ec2_instance",
    inferred_aws_region=aws_config.region,
    bound_iam_instance_profile_arns=[semantic_profile.arn],
    bound_ami_ids=[
        semantic_image.id
    ],  # Reference the new way of doing stuff, not the old one
    bound_account_ids=[aws_account.account_id],
    bound_vpc_ids=[apps_vpc["id"]],
    token_policies=[semantic_server_vault_policy.name],
)

## Begin block for migrating to pyinfra images
consul_datacenter = consul_stack.require_output("datacenter")
instance_tags = aws_config.merged_tags(
    {"Name": f"semantic-instance-{stack_info.env_suffix}"}
)
semantic_instance = ec2.Instance(
    f"semantic-instance-{stack_info.env_suffix}",
    ami=semantic_image.id,
    instance_type=get_config("semantic:instance_type"),
    iam_instance_profile=semantic_profile.id,
    tags=instance_tags,
    volume_tags=instance_tags,
    subnet_id=apps_vpc["subnet_ids"][1],
    key_name="oldevops",
    root_block_device=ec2.InstanceRootBlockDeviceArgs(
        volume_type=DiskTypes.ssd, volume_size=25
    ),
    vpc_security_group_ids=[
        semantic_server_security_group.id,
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
                        ]
                    },
                    sort_keys=True,
                )
            ).encode("utf8")
        ).decode("utf8")
    ),
    opts=ResourceOptions(depends_on=[semantic_db_consul_service]),
)
semantic_elastic_ip = ec2.Eip(
    "semantic-instance-elastic-ip",
    instance=semantic_instance.id,
    vpc=True,
)

fifteen_minutes = 60 * 15
semantic_domain = route53.Record(
    f"semantic-{stack_info.env_suffix}-service-domain",
    name=semantic_config.get("domain"),
    type="A",
    ttl=fifteen_minutes,
    records=[semantic_elastic_ip.public_ip],
    zone_id=mitodl_zone_id,
    opts=ResourceOptions(depends_on=[semantic_instance, semantic_elastic_ip]),
)
semantic_domain_v6 = route53.Record(
    f"semantic-{stack_info.env_suffix}-service-domain-v6",
    name=semantic_config.get("domain"),
    type="AAAA",
    ttl=fifteen_minutes,
    records=semantic_instance.ipv6_addresses,
    zone_id=mitodl_zone_id,
    opts=ResourceOptions(depends_on=[semantic_instance]),
)

# export(
#    "dagster_app",
#    },
