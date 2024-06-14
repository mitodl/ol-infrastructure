"""The complete state needed to provision Keycloak running on Docker."""

import base64
import json
import textwrap
from functools import partial
from itertools import chain
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
from pulumi_aws import acm, ec2, get_caller_identity, iam, route53
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
from ol_infrastructure.lib.aws.route53_helper import acm_certificate_validation_records
from ol_infrastructure.lib.consul import consul_key_helper, get_consul_provider
from ol_infrastructure.lib.ol_types import AWSBase
from ol_infrastructure.lib.pulumi_helper import parse_stack
from ol_infrastructure.lib.stack_defaults import defaults
from ol_infrastructure.lib.vault import setup_vault_provider

setup_vault_provider()

keycloak_config = Config("keycloak")
stack_info = parse_stack()

aws_account = get_caller_identity()

network_stack = StackReference(f"infrastructure.aws.network.{stack_info.name}")
policy_stack = StackReference("infrastructure.aws.policies")
dns_stack = StackReference("infrastructure.aws.dns")
consul_stack = StackReference(f"infrastructure.consul.operations.{stack_info.name}")
vault_stack = StackReference(f"infrastructure.vault.operations.{stack_info.name}")
vault_pki_stack = StackReference(f"substructure.vault.pki.operations.{stack_info.name}")

# target vpc is 'operations', for a non-app-specific service
target_vpc_name = keycloak_config.get("target_vpc") or f"{stack_info.env_prefix}_vpc"
target_vpc = network_stack.require_output(target_vpc_name)
target_vpc_id = target_vpc["id"]

data_vpc = network_stack.require_output("data_vpc")

mitol_zone_id = dns_stack.require_output("ol")["id"]
keycloak_domain = keycloak_config.get("domain")

# TODO MD 20230206  # noqa: FIX002, TD002, TD003, TD004
# This might be needed in the future but right now it just causes errors
secrets = read_yaml_secrets(Path(f"keycloak/data.{stack_info.env_suffix}.yaml"))
if secrets is None:
    msg = "You must create the secrets structure at src/bridge/secrets/keycloak/data.{stack_info.env_suffix}.yaml"  # noqa: E501
    raise ValueError(msg)

aws_config = AWSBase(
    tags={"OU": "operations", "Environment": f"operations-{stack_info.env_suffix}"}
)
consul_provider = get_consul_provider(stack_info)

env_name = f"{stack_info.env_prefix}-{stack_info.env_suffix}"

# IAM and instance profile
keycloak_instance_role = iam.Role(
    f"keycloak-instance-role-{env_name}",
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
    path="/ol-operations/keycloak/role/",
    tags=aws_config.tags,
)

iam.RolePolicyAttachment(
    f"keycloak-describe-instance-role-policy-{env_name}",
    policy_arn=policy_stack.require_output("iam_policies")["describe_instances"],
    role=keycloak_instance_role.name,
)
iam.RolePolicyAttachment(
    "keycloak-route53-records-permission",
    policy_arn=policy_stack.require_output("iam_policies")["route53_odl_zone_records"],
    role=keycloak_instance_role.name,
)

keycloak_server_instance_profile = iam.InstanceProfile(
    f"keycloak-instance-profile-{env_name}",
    role=keycloak_instance_role.name,
    path="/ol-operations/keycloak/profile/",
)

# Network Access Control

# Create various security groups
keycloak_server_security_group = ec2.SecurityGroup(
    f"keycloak-server-security-group-{env_name}",
    name=f"keycloak-server-{target_vpc_name}-{env_name}",
    description="Access control for keycloak servers",
    ingress=[
        ec2.SecurityGroupIngressArgs(
            protocol="tcp",
            from_port=DEFAULT_HTTPS_PORT,
            to_port=DEFAULT_HTTPS_PORT,
            cidr_blocks=["0.0.0.0/0"],
            description=(
                f"Allow traffic to the keycloak server on port {DEFAULT_HTTPS_PORT}"
            ),
        ),
        # https://infinispan.org/docs/stable/titles/security/security.html#ports_protocols
        ec2.SecurityGroupIngressArgs(
            self=True,
            from_port=7800,
            to_port=7800,
            protocol="tcp",
            description=(
                "Allow all keycloak servers to talk to all other keycloak servers on"
                " port 7800 tcp for IPSN clustering."
            ),
        ),
        ec2.SecurityGroupIngressArgs(
            self=True,
            from_port=7800,
            to_port=7800,
            protocol="udp",
            description=(
                "Allow all keycloak servers to talk to all other keycloak servers on"
                " port 7800 udp for IPSN clustering."
            ),
        ),
    ],
    egress=default_egress_args,
    vpc_id=target_vpc_id,
    tags=aws_config.tags,
)

keycloak_database_security_group = ec2.SecurityGroup(
    f"keycloak-database-security-group-{env_name}",
    name=f"keycloak-database-{target_vpc_name}-{env_name}",
    description="Access control for the keycloak database",
    ingress=[
        ec2.SecurityGroupIngressArgs(
            security_groups=[
                keycloak_server_security_group.id,
                consul_stack.require_output("security_groups")["consul_server"],
                vault_stack.require_output("vault_server")["security_group"],
                data_vpc["security_groups"]["integrator"],
            ],
            cidr_blocks=[target_vpc["cidr"]],
            protocol="tcp",
            from_port=DEFAULT_POSTGRES_PORT,
            to_port=DEFAULT_POSTGRES_PORT,
            description=(
                f"Access to Postgres from keycloak nodes on {DEFAULT_POSTGRES_PORT}"
            ),
        ),
    ],
    vpc_id=target_vpc_id,
    tags=aws_config.tags,
)

# Database
rds_defaults = defaults(stack_info)["rds"]

rds_password = keycloak_config.require("rds_password")

keycloak_db_config = OLPostgresDBConfig(
    instance_name=f"keycloak-{stack_info.env_suffix}",
    password=rds_password,
    storage=keycloak_config.get("db_capacity")
    or str(AWS_RDS_DEFAULT_DATABASE_CAPACITY),
    subnet_group_name=target_vpc["rds_subnet"],
    security_groups=[keycloak_database_security_group],
    engine_major_version="16",
    db_name="keycloak",
    tags=aws_config.tags,
    **rds_defaults,
)
keycloak_db = OLAmazonDB(keycloak_db_config)

db_address = keycloak_db.db_instance.address
db_port = keycloak_db.db_instance.port

keycloak_db_vault_backend_config = OLVaultPostgresDatabaseConfig(
    db_name=keycloak_db_config.db_name,
    mount_point=f"{keycloak_db_config.engine}-keycloak",
    db_admin_username=keycloak_db_config.username,
    db_admin_password=rds_password,
    db_host=db_address,
)
keycloak_db_vault_backend = OLVaultDatabaseBackend(keycloak_db_vault_backend_config)

keycloak_db_consul_node = Node(
    f"keycloak-{stack_info.env_suffix}-db-node",
    name="keycloak-postgres-db",
    address=db_address,
    opts=consul_provider,
)

keycloak_db_consul_service = Service(
    f"keycloak-{stack_info.env_suffix}-db-service",
    node=keycloak_db_consul_node.name,
    name="keycloak-postgres",
    port=db_port,
    meta={
        "external-node": True,
        "external-probe": True,
    },
    checks=[
        ServiceCheckArgs(
            check_id="keycloak-db",
            interval="10s",
            name="keycloak-db",
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

# Provision EC2 resources
instance_type_name = keycloak_config.get(
    "instance_type", InstanceTypes.general_purpose_large.name
)
instance_type = InstanceTypes.dereference(instance_type_name)

subnets = target_vpc["subnet_ids"]
subnet_id = subnets.apply(chain)

grafana_credentials = read_yaml_secrets(
    Path(f"vector/grafana.{stack_info.env_suffix}.yaml")
)
consul_datacenter = consul_stack.require_output("datacenter")

instance_tags = aws_config.merged_tags({"Name": f"keycloak-{stack_info.env_suffix}"})

keycloak_server_ami = ec2.get_ami(
    filters=[
        {
            "name": "tag:Name",
            "values": ["keycloak-server"],
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

# Create an auto-scale group for web application servers
keycloak_web_acm_cert = acm.Certificate(
    "keycloak-load-balancer-acm-certificate",
    domain_name=keycloak_domain,
    validation_method="DNS",
    tags=aws_config.tags,
)

keycloak_acm_cert_validation_records = (
    keycloak_web_acm_cert.domain_validation_options.apply(
        partial(
            acm_certificate_validation_records,
            zone_id=mitol_zone_id,
            stack_info=stack_info,
        )
    )
)

keycloak_web_acm_validated_cert = acm.CertificateValidation(
    "wait-for-keycloak-acm-cert-validation",
    certificate_arn=keycloak_web_acm_cert.arn,
    validation_record_fqdns=keycloak_acm_cert_validation_records.apply(
        lambda validation_records: [
            validation_record.fqdn for validation_record in validation_records
        ]
    ),
)
keycloak_lb_config = OLLoadBalancerConfig(
    enable_insecure_http=False,
    listener_cert_domain=keycloak_domain,
    listener_use_acm=True,
    listener_cert_arn=keycloak_web_acm_cert.arn,
    security_groups=[keycloak_server_security_group],
    subnets=subnets,
    tags=instance_tags,
)

keycloak_tg_config = OLTargetGroupConfig(
    vpc_id=target_vpc["id"],
    target_group_healthcheck=False,
    health_check_interval=60,
    health_check_matcher="404",  # TODO Figure out a real endpoint for this  # noqa: E501, FIX002, TD002, TD003, TD004
    health_check_path="/ping",
    stickiness="lb_cookie",
    tags=instance_tags,
)

keycloak_lt_config = OLLaunchTemplateConfig(
    block_device_mappings=block_device_mappings,
    image_id=keycloak_server_ami.id,
    instance_type=instance_type,
    instance_profile_arn=keycloak_server_instance_profile.arn,
    security_groups=[
        target_vpc["security_groups"]["default"],
        keycloak_server_security_group,
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
                            APPLICATION=keycloak
                            SERVICE=sso
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
                        ],
                    }
                ),
            ).encode("utf8")
        ).decode("utf8")
    ),
)

keycloak_autoscale_sizes = keycloak_config.get_object("auto_scale") or {
    "desired": 2,
    "min": 1,
    "max": 3,
}
keycloak_asg_config = OLAutoScaleGroupConfig(
    asg_name=f"keycloak-{stack_info.env_suffix}",
    aws_config=aws_config,
    desired_size=keycloak_autoscale_sizes["desired"],
    min_size=keycloak_autoscale_sizes["min"],
    max_size=keycloak_autoscale_sizes["max"],
    vpc_zone_identifiers=target_vpc["subnet_ids"],
    tags=instance_tags,
)

autoscale_setup = OLAutoScaling(
    asg_config=keycloak_asg_config,
    lt_config=keycloak_lt_config,
    tg_config=keycloak_tg_config,
    lb_config=keycloak_lb_config,
)

# Vault policy definition
keycloak_server_vault_policy = vault.Policy(
    "keycloak-server-vault-policy",
    name="keycloak-server",
    policy=Path(__file__).parent.joinpath("keycloak_server_policy.hcl").read_text(),
)

vault.aws.AuthBackendRole(
    "keycloak-server-ec2-vault-auth",
    backend="aws",
    auth_type="iam",
    role="keycloak-server",
    inferred_entity_type="ec2_instance",
    inferred_aws_region=aws_config.region,
    bound_iam_instance_profile_arns=[keycloak_server_instance_profile.arn],
    bound_ami_ids=[keycloak_server_ami.id],
    bound_account_ids=[aws_account.account_id],
    bound_vpc_ids=[target_vpc_id],
    token_policies=[keycloak_server_vault_policy.name],
)

# Vault KV2 mount definition
keycloak_server_vault_mount = vault.Mount(
    "keycloak-server-configuration-secrets-mount",
    path="secret-keycloak",
    type="kv-v2",
    options={"version": 2},
    description="Storage of configuration credentials and secrets used by keycloak",
    opts=ResourceOptions(delete_before_replace=True),
)


keycloak_server_secrets = vault.generic.Secret(
    "keycloak-server-configuration-secrets",
    path=keycloak_server_vault_mount.path.apply("{}/keycloak-secrets".format),
    data_json=json.dumps(secrets),
)

consul_keys = {
    "keycloak/keycloak_host": keycloak_domain,
    "keycloak/rds_host": db_address,
}
consul.Keys(
    "keycloak-server-configuration-data",
    keys=consul_key_helper(consul_keys),
    opts=consul_provider,
)

# Create Route53 DNS records
five_minutes = 60 * 5
route53.Record(
    f"keycloak-server-dns-record-{keycloak_domain}",
    name=keycloak_domain,
    type="CNAME",
    ttl=five_minutes,
    records=[autoscale_setup.load_balancer.dns_name],
    zone_id=mitol_zone_id,
)

# TODO MD 20230206 revisit this, probably need to export more things  # noqa: E501, FIX002, TD002, TD003, TD004
export(
    "keycloak",
    {
        "rds_host": db_address,
    },
)
