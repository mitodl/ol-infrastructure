import base64
import json
import textwrap
from functools import partial
from pathlib import Path

import pulumi_consul as consul
import pulumi_vault as vault
import yaml
from pulumi import Alias, Config, Output, ResourceOptions, StackReference, export
from pulumi_aws import acm, ec2, get_caller_identity, iam, route53, ses

from bridge.lib.magic_numbers import (
    DEFAULT_POSTGRES_PORT,
    DEFAULT_REDIS_PORT,
    FIVE_MINUTES,
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
from ol_infrastructure.lib.aws.ec2_helper import InstanceTypes
from ol_infrastructure.lib.aws.iam_helper import IAM_POLICY_VERSION, lint_iam_policy
from ol_infrastructure.lib.aws.route53_helper import acm_certificate_validation_records
from ol_infrastructure.lib.consul import get_consul_provider
from ol_infrastructure.lib.ol_types import AWSBase
from ol_infrastructure.lib.pulumi_helper import parse_stack
from ol_infrastructure.lib.stack_defaults import defaults
from ol_infrastructure.lib.vault import setup_vault_provider

setup_vault_provider()
superset_config = Config("superset")
stack_info = parse_stack()
consul_provider = get_consul_provider(stack_info)
network_stack = StackReference(f"infrastructure.aws.network.{stack_info.name}")
dns_stack = StackReference("infrastructure.aws.dns")
consul_stack = StackReference(f"infrastructure.consul.data.{stack_info.name}")
vault_infra_stack = StackReference(f"infrastructure.vault.operations.{stack_info.name}")
vault_mount_stack = StackReference(
    f"substructure.vault.static_mounts.operations.{stack_info.name}"
)
policy_stack = StackReference("infrastructure.aws.policies")
mitol_zone_id = dns_stack.require_output("ol")["id"]
operations_vpc = network_stack.require_output("operations_vpc")
data_vpc = network_stack.require_output("data_vpc")
superset_env = f"data-{stack_info.env_suffix}"
superset_vault_kv_path = vault_mount_stack.require_output("superset_kv")["path"]
aws_config = AWSBase(tags={"OU": "data", "Environment": superset_env})
consul_security_groups = consul_stack.require_output("security_groups")

aws_account = get_caller_identity()
superset_domain = superset_config.get("domain")
superset_mail_domain = f"mail.{superset_domain}"
# Create IAM role

superset_bucket_name = f"ol-superset-{stack_info.env_suffix}"
# Create instance profile for granting access to S3 buckets
superset_iam_policy = iam.Policy(
    f"superset-policy-{stack_info.env_suffix}",
    name=f"superset-policy-{stack_info.env_suffix}",
    path=f"/ol-data/superset-policy-{stack_info.env_suffix}/",
    policy=lint_iam_policy(
        policy_document=json.dumps(
            {
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
                            "s3:ListBucket*",
                            "s3:GetObject",
                            "s3:PutObject",
                            "s3:DeleteObject*",
                        ],
                        "Resource": [
                            f"arn:aws:s3:::{superset_bucket_name}",
                            f"arn:aws:s3:::{superset_bucket_name}/*",
                        ],
                    },
                    {
                        "Effect": "Allow",
                        "Action": ["ses:SendEmail", "ses:SendRawEmail"],
                        "Resource": [
                            "arn:*:ses:*:*:identity/*mit.edu",
                            f"arn:aws:ses:*:*:configuration-set/superset-{superset_env}",
                        ],
                    },
                    {
                        "Effect": "Allow",
                        "Action": ["ses:GetSendQuota"],
                        "Resource": "*",
                    },
                ],
            }
        ),
        stringify=True,
    ),
    description="Policy for granting acces for batch data workflows to AWS resources",
)

superset_instance_role = iam.Role(
    "superset-instance-role",
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
    name=f"superset-instance-role-{stack_info.env_suffix}",
    path="/ol-data/superset-role/",
    tags=aws_config.tags,
)

iam.RolePolicyAttachment(
    f"superset-role-policy-{stack_info.env_suffix}",
    policy_arn=superset_iam_policy.arn,
    role=superset_instance_role.name,
)

iam.RolePolicyAttachment(
    f"superset-describe-instance-role-policy-{stack_info.env_suffix}",
    policy_arn=policy_stack.require_output("iam_policies")["describe_instances"],
    role=superset_instance_role.name,
)

iam.RolePolicyAttachment(
    f"concourse-route53-role-policy-{stack_info.env_suffix}",
    policy_arn=policy_stack.require_output("iam_policies")["route53_ol_zone_records"],
    role=superset_instance_role.name,
)

superset_profile = iam.InstanceProfile(
    f"superset-instance-profile-{stack_info.env_suffix}",
    role=superset_instance_role.name,
    name=f"superset-instance-profile-{stack_info.env_suffix}",
    path="/ol-data/superset-profile/",
)

superset_security_group = ec2.SecurityGroup(
    "superset-security-group",
    name_prefix=f"superset-{superset_env}-",
    description="Allow Superset to connect to RDS and Elasticache",
    vpc_id=data_vpc["id"],
    ingress=[],
    egress=[],
    tags=aws_config.merged_tags(
        {"Name": f"superset-{superset_env}"},
    ),
)

# Get the AMI ID for the superset/docker-compose image
superset_ami = ec2.get_ami(
    filters=[
        ec2.GetAmiFilterArgs(name="name", values=["superset-server-*"]),
        ec2.GetAmiFilterArgs(name="virtualization-type", values=["hvm"]),
        ec2.GetAmiFilterArgs(name="root-device-type", values=["ebs"]),
    ],
    most_recent=True,
    owners=[aws_account.account_id],
)

# Create a vault policy to allow superset to get to the secrets it needs
superset_server_vault_policy = vault.Policy(
    "superset-server-vault-policy",
    name="superset-server",
    policy=Path(__file__).parent.joinpath("superset_server_policy.hcl").read_text(),
)
# Register Superset AMI for Vault AWS auth
vault.aws.AuthBackendRole(
    "superset-server-ami-ec2-vault-auth",
    backend="aws",
    auth_type="ec2",
    role="superset",
    inferred_aws_region=aws_config.region,
    bound_iam_instance_profile_arns=[superset_profile.arn],
    bound_ami_ids=[
        superset_ami.id
    ],  # Reference the new way of doing stuff, not the old one
    bound_account_ids=[aws_account.account_id],
    bound_vpc_ids=[data_vpc["id"]],
    token_policies=[superset_server_vault_policy.name],
    opts=ResourceOptions(delete_before_replace=True),
)

superset_secrets = read_yaml_secrets(
    Path(f"superset/data.{stack_info.env_suffix}.yaml")
)
for path, data in superset_secrets.items():
    vault.kv.SecretV2(
        f"superset-vault-secret-{path}",
        mount=superset_vault_kv_path,
        name=path,
        data_json=json.dumps(data),
    )

########################################
# Create SES Service For superset Emails #
########################################

superset_ses_domain_identity = ses.DomainIdentity(
    "superset-ses-domain-identity",
    domain=superset_mail_domain,
)
superset_ses_verification_record = route53.Record(
    "superset-ses-domain-identity-verification-dns-record",
    zone_id=mitol_zone_id,
    name=superset_ses_domain_identity.id.apply("_amazonses.{}".format),
    type="TXT",
    ttl=FIVE_MINUTES,
    records=[superset_ses_domain_identity.verification_token],
)
superset_ses_domain_identity_verification = ses.DomainIdentityVerification(
    "superset-ses-domain-identity-verification-resource",
    domain=superset_ses_domain_identity.id,
    opts=ResourceOptions(depends_on=[superset_ses_verification_record]),
)
superset_mail_from_domain = ses.MailFrom(
    "superset-ses-mail-from-domain",
    domain=superset_ses_domain_identity_verification.domain,
    mail_from_domain=superset_ses_domain_identity_verification.domain.apply(
        "bounce.{}".format
    ),
)
superset_mail_from_address = ses.EmailIdentity(
    "superset-ses-mail-from-identity",
    email=superset_config.require("sender_email_address"),
)
# Example Route53 MX record
superset_ses_domain_mail_from_mx = route53.Record(
    f"superset-ses-mail-from-mx-record-for-{superset_env}",
    zone_id=mitol_zone_id,
    name=superset_mail_from_domain.mail_from_domain,
    type="MX",
    ttl=FIVE_MINUTES,
    records=["10 feedback-smtp.us-east-1.amazonses.com"],
)
ses_domain_mail_from_txt = route53.Record(
    "superset-ses-domain-mail-from-text-record",
    zone_id=mitol_zone_id,
    name=superset_mail_from_domain.mail_from_domain,
    type="TXT",
    ttl=FIVE_MINUTES,
    records=["v=spf1 include:amazonses.com -all"],
)
superset_ses_domain_dkim = ses.DomainDkim(
    "superset-ses-domain-dkim", domain=superset_ses_domain_identity.domain
)
for loop_counter in range(3):
    route53.Record(
        f"superset-ses-domain-dkim-record-{loop_counter}",
        zone_id=mitol_zone_id,
        name=superset_ses_domain_dkim.dkim_tokens[loop_counter].apply(
            lambda dkim_name: f"{dkim_name}._domainkey.{superset_mail_domain}"
        ),
        type="CNAME",
        ttl=FIVE_MINUTES,
        records=[
            superset_ses_domain_dkim.dkim_tokens[loop_counter].apply(
                "{}.dkim.amazonses.com".format
            )
        ],
    )
superset_ses_configuration_set = ses.ConfigurationSet(
    "superset-ses-configuration-set",
    reputation_metrics_enabled=True,
    sending_enabled=True,
    name=f"superset-{superset_env}",
)
superset_ses_event_destintations = ses.EventDestination(
    "superset-ses-event-destination-routing",
    configuration_set_name=superset_ses_configuration_set.name,
    enabled=True,
    matching_types=[
        "send",
        "reject",
        "bounce",
        "complaint",
        "delivery",
        "open",
        "click",
        "renderingFailure",
    ],
    cloudwatch_destinations=[
        ses.EventDestinationCloudwatchDestinationArgs(
            default_value="default",
            dimension_name=f"superset-{superset_env}",
            value_source="emailHeader",
        )
    ],
)

# Create RDS Postgres instance and connect with Vault
superset_db_security_group = ec2.SecurityGroup(
    "superset-rds-security-group",
    name_prefix=f"superset-rds-{superset_env}-",
    description="Grant access to RDS from Superset",
    ingress=[
        ec2.SecurityGroupIngressArgs(
            from_port=DEFAULT_POSTGRES_PORT,
            to_port=DEFAULT_POSTGRES_PORT,
            protocol="tcp",
            security_groups=[
                superset_security_group.id,
                vault_infra_stack.require_output("vault_server")["security_group"],
            ],
            description="Grant access to RDS from Superset",
        ),
    ],
    tags=aws_config.merged_tags({"Name": f"superset-rds-{superset_env}"}),
    vpc_id=data_vpc["id"],
)
rds_defaults = defaults(stack_info)["rds"]
rds_defaults["use_blue_green"] = False
superset_db_config = OLPostgresDBConfig(
    instance_name=f"ol-superset-db-{stack_info.env_suffix}",
    password=superset_config.require("db_password"),
    subnet_group_name=data_vpc["rds_subnet"],
    security_groups=[superset_db_security_group],
    engine_major_version="16",
    tags=aws_config.tags,
    db_name="superset",
    **rds_defaults,
)
superset_db = OLAmazonDB(superset_db_config)

superset_vault_db_config = OLVaultPostgresDatabaseConfig(
    db_name=superset_db_config.db_name,
    mount_point=f"{superset_db_config.engine}-superset",
    db_admin_username=superset_db_config.username,
    db_admin_password=superset_config.require("db_password"),
    db_host=superset_db.db_instance.address,
)
superset_db_vault_backend = OLVaultDatabaseBackend(superset_vault_db_config)

superset_db_consul_node = consul.Node(
    "superset-instance-db-node",
    name="superset-postgres-db",
    address=superset_db.db_instance.address,
    datacenter=superset_env,
    opts=consul_provider,
)

superset_db_consul_service = consul.Service(
    "superset-instance-db-service",
    node=superset_db_consul_node.name,
    name="superset-db",
    port=superset_db_config.port,
    meta={
        "external-node": True,
        "external-probe": True,
    },
    checks=[
        consul.ServiceCheckArgs(
            check_id="superset-instance-db",
            interval="10s",
            name="superset-instance-id",
            timeout="60s",
            status="passing",
            tcp=superset_db.db_instance.address.apply(
                lambda address: f"{address}:{superset_db_config.port}"
            ),
        )
    ],
    opts=consul_provider,
)

# Create an Elasticache cluster for Redis caching and Celery broker
redis_config = Config("redis")
redis_cluster_security_group = ec2.SecurityGroup(
    f"superset-redis-cluster-{superset_env}",
    name_prefix=f"superset-redis-{superset_env}-",
    description="Grant access to Redis from Open edX",
    ingress=[
        ec2.SecurityGroupIngressArgs(
            from_port=DEFAULT_REDIS_PORT,
            to_port=DEFAULT_REDIS_PORT,
            protocol="tcp",
            security_groups=[
                superset_security_group.id,
                operations_vpc["security_groups"]["celery_monitoring"],
            ],
            description="Allow access from edX & celery monitoring to Redis for"
            "caching and queueing",
        )
    ],
    tags=aws_config.merged_tags({"Name": f"superset-redis-{superset_env}"}),
    vpc_id=data_vpc["id"],
)

redis_instance_type = (
    redis_config.get("instance_type") or defaults(stack_info)["redis"]["instance_type"]
)
redis_auth_token = superset_secrets["redis"]["token"]
redis_cache_config = OLAmazonRedisConfig(
    encrypt_transit=True,
    auth_token=redis_auth_token,
    cluster_mode_enabled=False,
    encrypted=True,
    engine_version="7.2",
    engine="valkey",
    instance_type=redis_instance_type,
    num_instances=3,
    shard_count=1,
    auto_upgrade=True,
    cluster_description="Redis cluster for edX platform tasks and caching",
    cluster_name=f"superset-redis-{superset_env}",
    security_groups=[redis_cluster_security_group.id],
    subnet_group=data_vpc[
        "elasticache_subnet"
    ],  # the name of the subnet group created in the OLVPC component resource
    tags=aws_config.tags,
)
superset_redis_cache = OLAmazonCache(
    redis_cache_config,
    opts=ResourceOptions(
        aliases=[Alias(name=f"superset-redis-{superset_env}-redis-elasticache-cluster")]
    ),
)
superset_redis_consul_node = consul.Node(
    "superset-redis-cache-node",
    name="superset-redis",
    address=superset_redis_cache.address,
    opts=consul_provider,
)

superset_redis_consul_service = consul.Service(
    "superset-redis-consul-service",
    node=superset_redis_consul_node.name,
    name="superset-redis",
    port=redis_cache_config.port,
    meta={
        "external-node": True,
        "external-probe": True,
    },
    checks=[
        consul.ServiceCheckArgs(
            check_id="superset-redis",
            interval="10s",
            name="superset-redis",
            timeout="1m0s",
            status="passing",
            tcp=Output.all(
                address=superset_redis_cache.address,
                port=superset_redis_cache.cache_cluster.port,
            ).apply(lambda cluster: "{address}:{port}".format(**cluster)),
        )
    ],
    opts=consul_provider,
)

# Create an auto-scale group for web application servers
superset_web_acm_cert = acm.Certificate(
    "superset-load-balancer-acm-certificate",
    domain_name=superset_domain,
    validation_method="DNS",
    tags=aws_config.tags,
)

superset_acm_cert_validation_records = (
    superset_web_acm_cert.domain_validation_options.apply(
        partial(
            acm_certificate_validation_records,
            cert_name="superset",
            zone_id=mitol_zone_id,
            stack_info=stack_info,
        )
    )
)

superset_web_acm_validated_cert = acm.CertificateValidation(
    "wait-for-superset-acm-cert-validation",
    certificate_arn=superset_web_acm_cert.arn,
    validation_record_fqdns=superset_acm_cert_validation_records.apply(
        lambda validation_records: [
            validation_record.fqdn for validation_record in validation_records
        ]
    ),
)
superset_lb_config = OLLoadBalancerConfig(
    subnets=data_vpc["subnet_ids"],
    security_groups=[data_vpc["security_groups"]["web"]],
    tags=aws_config.merged_tags({"Name": f"superset-lb-{stack_info.env_suffix}"}),
    listener_cert_domain=superset_domain,
    listener_cert_arn=superset_web_acm_cert.arn,
)

superset_tg_config = OLTargetGroupConfig(
    vpc_id=data_vpc["id"],
    health_check_interval=60,
    health_check_matcher="200-399",
    health_check_path="/health",
    health_check_unhealthy_threshold=3,  # give extra time for Superset to start up
    tags=aws_config.merged_tags({"Name": f"superset-tg-{stack_info.env_suffix}"}),
)

consul_datacenter = consul_stack.require_output("datacenter")
grafana_credentials = read_yaml_secrets(
    Path(f"vector/grafana.{stack_info.env_suffix}.yaml")
)

superset_web_block_device_mappings = [BlockDeviceMapping(volume_size=50)]
superset_web_tag_specs = [
    TagSpecification(
        resource_type="instance",
        tags=aws_config.merged_tags({"Name": f"superset-web-{stack_info.env_suffix}"}),
    ),
    TagSpecification(
        resource_type="volume",
        tags=aws_config.merged_tags({"Name": f"superset-web-{stack_info.env_suffix}"}),
    ),
]

superset_web_lt_config = OLLaunchTemplateConfig(
    block_device_mappings=superset_web_block_device_mappings,
    image_id=superset_ami.id,
    instance_type=superset_config.get("web_instance_type")
    or InstanceTypes.burstable_medium,
    instance_profile_arn=superset_profile.arn,
    security_groups=[
        superset_security_group.id,
        consul_security_groups["consul_agent"],
        data_vpc["security_groups"]["web"],
    ],
    tags=aws_config.merged_tags({"Name": f"superset-web-{stack_info.env_suffix}"}),
    tag_specifications=superset_web_tag_specs,
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
                            APPLICATION=superset
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
                                "path": "/etc/docker/compose/.env",
                                "content": f"DOMAIN={superset_domain}\nVAULT_ADDR=https://vault-{stack_info.env_suffix}.odl.mit.edu\n",
                                "append": True,
                            },
                            {
                                "path": "/etc/default/docker-compose",
                                "content": "COMPOSE_PROFILES=web",
                            },
                            {
                                "path": "/etc/profile",
                                "content": "export COMPOSE_PROFILES=web",
                                "append": True,
                            },
                        ]
                    },
                    sort_keys=True,
                )
            ).encode("utf8")
        ).decode("utf8")
    ),
)

superset_web_auto_scale_config = superset_config.get_object("web_auto_scale") or {
    "desired": 1,
    "min": 1,
    "max": 2,
}
superset_web_asg_config = OLAutoScaleGroupConfig(
    asg_name=f"superset-web-{superset_env}",
    aws_config=aws_config,
    health_check_grace_period=300,
    instance_refresh_warmup=300,
    desired_size=superset_web_auto_scale_config["desired"],
    min_size=superset_web_auto_scale_config["min"],
    max_size=superset_web_auto_scale_config["max"],
    vpc_zone_identifiers=data_vpc["subnet_ids"],
    tags=aws_config.merged_tags({"Name": f"superset-web-{superset_env}"}),
)

superset_web_asg = OLAutoScaling(
    asg_config=superset_web_asg_config,
    lt_config=superset_web_lt_config,
    tg_config=superset_tg_config,
    lb_config=superset_lb_config,
)


# Create an auto-scale group for Celery workers
superset_worker_block_device_mappings = [BlockDeviceMapping(volume_size=50)]
superset_worker_tag_specs = [
    TagSpecification(
        resource_type="instance",
        tags=aws_config.merged_tags(
            {"Name": f"superset-worker-{stack_info.env_suffix}"}
        ),
    ),
    TagSpecification(
        resource_type="volume",
        tags=aws_config.merged_tags(
            {"Name": f"superset-worker-{stack_info.env_suffix}"}
        ),
    ),
]

superset_worker_lt_config = OLLaunchTemplateConfig(
    block_device_mappings=superset_worker_block_device_mappings,
    image_id=superset_ami.id,
    instance_type=superset_config.get("worker_instance_type")
    or InstanceTypes.burstable_medium,
    instance_profile_arn=superset_profile.arn,
    security_groups=[
        superset_security_group.id,
        consul_security_groups["consul_agent"],
    ],
    tags=aws_config.merged_tags({"Name": f"superset-worker-{stack_info.env_suffix}"}),
    tag_specifications=superset_worker_tag_specs,
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
                            APPLICATION=superset
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
                                "path": "/etc/default/docker-compose",
                                "content": "COMPOSE_PROFILES=worker",
                            },
                            {
                                "path": "/etc/profile",
                                "content": "export COMPOSE_PROFILES=worker",
                                "append": True,
                            },
                            {
                                "path": "/etc/docker/compose/.env",
                                "content": f"DOMAIN={superset_domain}\nVAULT_ADDR=https://vault-{stack_info.env_suffix}.odl.mit.edu\n",
                                "append": True,
                            },
                        ]
                    },
                    sort_keys=True,
                )
            ).encode("utf8")
        ).decode("utf8")
    ),
)

superset_worker_auto_scale_config = superset_config.get_object("worker_auto_scale") or {
    "desired": 1,
    "min": 1,
    "max": 2,
}
superset_worker_asg_config = OLAutoScaleGroupConfig(
    asg_name=f"superset-worker-{superset_env}",
    aws_config=aws_config,
    health_check_type="EC2",
    desired_size=superset_worker_auto_scale_config["desired"],
    min_size=superset_worker_auto_scale_config["min"],
    max_size=superset_worker_auto_scale_config["max"],
    vpc_zone_identifiers=data_vpc["subnet_ids"],
    tags=aws_config.merged_tags({"Name": f"superset-worker-{superset_env}"}),
)

supserset_worker_asg = OLAutoScaling(
    asg_config=superset_worker_asg_config,
    lt_config=superset_worker_lt_config,
)


# Create Route53 DNS records for Superset
five_minutes = 60 * 5
route53.Record(
    "superset-server-dns-record",
    name=superset_config.require("domain"),
    type="CNAME",
    ttl=five_minutes,
    records=[superset_web_asg.load_balancer.dns_name],
    zone_id=mitol_zone_id,
    opts=ResourceOptions(delete_before_replace=True),
)

export(
    "superset",
    {
        "deployment": stack_info.env_prefix,
        "redis": superset_redis_cache.address,
        "redis_token": redis_auth_token,
    },
)
