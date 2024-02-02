import base64
import json
import textwrap
from functools import partial
from pathlib import Path

import pulumi_consul as consul
import pulumi_vault as vault
import yaml
from bridge.lib.magic_numbers import (
    DEFAULT_REDIS_PORT,
    FIVE_MINUTES,
)
from bridge.secrets.sops import read_yaml_secrets
from pulumi import Config, Output, ResourceOptions, StackReference
from pulumi_aws import acm, ec2, get_caller_identity, iam, route53, ses

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
from ol_infrastructure.lib.aws.ec2_helper import InstanceTypes
from ol_infrastructure.lib.aws.iam_helper import IAM_POLICY_VERSION, lint_iam_policy
from ol_infrastructure.lib.aws.route53_helper import acm_certificate_validation_records
from ol_infrastructure.lib.consul import get_consul_provider
from ol_infrastructure.lib.ol_types import AWSBase
from ol_infrastructure.lib.pulumi_helper import parse_stack
from ol_infrastructure.lib.stack_defaults import defaults
from ol_infrastructure.lib.vault import setup_vault_provider

setup_vault_provider()
leek_config = Config("leek")
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
data_vpc = network_stack.require_output("data_vpc")
leek_env = f"data-{stack_info.env_suffix}"
leek_vault_kv_path = vault_mount_stack.require_output("leek_kv")["path"]
aws_config = AWSBase(tags={"OU": "data", "Environment": leek_env})
consul_security_groups = consul_stack.require_output("security_groups")

aws_account = get_caller_identity()
leek_domain = leek_config.get("domain")
leek_mail_domain = f"mail.{leek_domain}"
# Create IAM role

leek_bucket_name = f"ol-leek-{stack_info.env_suffix}"
# Create instance profile for granting access to S3 buckets
leek_iam_policy = iam.Policy(
    f"leek-policy-{stack_info.env_suffix}",
    name=f"leek-policy-{stack_info.env_suffix}",
    path=f"/ol-data/leek-policy-{stack_info.env_suffix}/",
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
                            f"arn:aws:s3:::{leek_bucket_name}",
                            f"arn:aws:s3:::{leek_bucket_name}/*",
                        ],
                    },
                    {
                        "Effect": "Allow",
                        "Action": ["ses:SendEmail", "ses:SendRawEmail"],
                        "Resource": [
                            "arn:*:ses:*:*:identity/*mit.edu",
                            f"arn:aws:ses:*:*:configuration-set/leek-{leek_env}",
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

leek_instance_role = iam.Role(
    "leek-instance-role",
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
    name=f"leek-instance-role-{stack_info.env_suffix}",
    path="/ol-data/leek-role/",
    tags=aws_config.tags,
)

iam.RolePolicyAttachment(
    f"leek-role-policy-{stack_info.env_suffix}",
    policy_arn=leek_iam_policy.arn,
    role=leek_instance_role.name,
)

iam.RolePolicyAttachment(
    f"leek-describe-instance-role-policy-{stack_info.env_suffix}",
    policy_arn=policy_stack.require_output("iam_policies")["describe_instances"],
    role=leek_instance_role.name,
)

iam.RolePolicyAttachment(
    f"concourse-route53-role-policy-{stack_info.env_suffix}",
    policy_arn=policy_stack.require_output("iam_policies")["route53_ol_zone_records"],
    role=leek_instance_role.name,
)

leek_profile = iam.InstanceProfile(
    f"leek-instance-profile-{stack_info.env_suffix}",
    role=leek_instance_role.name,
    name=f"leek-instance-profile-{stack_info.env_suffix}",
    path="/ol-data/leek-profile/",
)

leek_security_group = ec2.SecurityGroup(
    "leek-security-group",
    name_prefix=f"leek-{leek_env}-",
    description="Allow leek to connect to Elasticache",
    vpc_id=data_vpc["id"],
    ingress=[],
    egress=[],
    tags=aws_config.merged_tags(
        {"Name": f"leek-{leek_env}"},
    ),
)

# Get the AMI ID for the leek/docker-compose image
leek_ami = ec2.get_ami(
    filters=[
        ec2.GetAmiFilterArgs(name="name", values=["leek-server-*"]),
        ec2.GetAmiFilterArgs(name="virtualization-type", values=["hvm"]),
        ec2.GetAmiFilterArgs(name="root-device-type", values=["ebs"]),
    ],
    most_recent=True,
    owners=[aws_account.account_id],
)

# Create a vault policy to allow leek to get to the secrets it needs
leek_server_vault_policy = vault.Policy(
    "leek-server-vault-policy",
    name="leek-server",
    policy=Path(__file__).parent.joinpath("leek_server_policy.hcl").read_text(),
)
# Register leek AMI for Vault AWS auth
vault.aws.AuthBackendRole(
    "leek-server-ami-ec2-vault-auth",
    backend="aws",
    auth_type="ec2",
    role="leek",
    inferred_aws_region=aws_config.region,
    bound_iam_instance_profile_arns=[leek_profile.arn],
    bound_ami_ids=[
        leek_ami.id
    ],  # Reference the new way of doing stuff, not the old one
    bound_account_ids=[aws_account.account_id],
    bound_vpc_ids=[data_vpc["id"]],
    token_policies=[leek_server_vault_policy.name],
    opts=ResourceOptions(delete_before_replace=True),
)

monitored_aws_apps = {
    "odl_video": read_yaml_secrets(Path(f"leek/data.{stack_info.env_suffix}.yaml")),
    "edxapp-mitxonline": read_yaml_secrets(Path(f"leek/data.{stack_info.env}")),
}


leek_secrets = read_yaml_secrets(Path(f"leek/data.{stack_info.env_suffix}.yaml"))
celery_brokers = leek_config.get_object("monitored_brokers", [])
leek_agent_subscriptions = []
for broker in celery_brokers:
    broker_config = {
        "broker": f"{broker['protocol']}://{[broker['username'], broker['password']]}@{broker['host']}:{broker['port']}",
        "broker_management_url": "http://mq:15672",
        "backend": None,
        "exchange": "celeryev",
        "queue": "leek.fanout",
        "routing_key": "#",
        "org_name": "mono",
        "app_name": "leek",
        "app_env": "prod",
        "prefetch_count": 1000,
        "concurrency_pool_size": 2,
        "batch_max_size_in_mb": 1,
        "batch_max_number_of_messages": 1000,
        "batch_max_window_in_seconds": 5,
    }
    leek_agent_subscriptions.append(broker_config)
for path, data in leek_secrets.items():
    vault.kv.SecretV2(
        f"leek-vault-secret-{path}",
        mount=leek_vault_kv_path,
        name=path,
        data_json=json.dumps(data),
    )

########################################
# Create SES Service For leek Emails #
########################################

leek_ses_domain_identity = ses.DomainIdentity(
    "leek-ses-domain-identity",
    domain=leek_mail_domain,
)
leek_ses_verification_record = route53.Record(
    "leek-ses-domain-identity-verification-dns-record",
    zone_id=mitol_zone_id,
    name=leek_ses_domain_identity.id.apply("_amazonses.{}".format),
    type="TXT",
    ttl=FIVE_MINUTES,
    records=[leek_ses_domain_identity.verification_token],
)
leek_ses_domain_identity_verification = ses.DomainIdentityVerification(
    "leek-ses-domain-identity-verification-resource",
    domain=leek_ses_domain_identity.id,
    opts=ResourceOptions(depends_on=[leek_ses_verification_record]),
)
leek_mail_from_domain = ses.MailFrom(
    "leek-ses-mail-from-domain",
    domain=leek_ses_domain_identity_verification.domain,
    mail_from_domain=leek_ses_domain_identity_verification.domain.apply(
        "bounce.{}".format
    ),
)
leek_mail_from_address = ses.EmailIdentity(
    "leek-ses-mail-from-identity",
    email=leek_config.require("sender_email_address"),
)
# Example Route53 MX record
leek_ses_domain_mail_from_mx = route53.Record(
    f"leek-ses-mail-from-mx-record-for-{leek_env}",
    zone_id=mitol_zone_id,
    name=leek_mail_from_domain.mail_from_domain,
    type="MX",
    ttl=FIVE_MINUTES,
    records=["10 feedback-smtp.us-east-1.amazonses.com"],
)
ses_domain_mail_from_txt = route53.Record(
    "leek-ses-domain-mail-from-text-record",
    zone_id=mitol_zone_id,
    name=leek_mail_from_domain.mail_from_domain,
    type="TXT",
    ttl=FIVE_MINUTES,
    records=["v=spf1 include:amazonses.com -all"],
)
leek_ses_domain_dkim = ses.DomainDkim(
    "leek-ses-domain-dkim", domain=leek_ses_domain_identity.domain
)
for loop_counter in range(3):
    route53.Record(
        f"leek-ses-domain-dkim-record-{loop_counter}",
        zone_id=mitol_zone_id,
        name=leek_ses_domain_dkim.dkim_tokens[loop_counter].apply(
            lambda dkim_name: f"{dkim_name}._domainkey.{leek_mail_domain}"
        ),
        type="CNAME",
        ttl=FIVE_MINUTES,
        records=[
            leek_ses_domain_dkim.dkim_tokens[loop_counter].apply(
                "{}.dkim.amazonses.com".format
            )
        ],
    )
leek_ses_configuration_set = ses.ConfigurationSet(
    "leek-ses-configuration-set",
    reputation_metrics_enabled=True,
    sending_enabled=True,
    name=f"leek-{leek_env}",
)
leek_ses_event_destintations = ses.EventDestination(
    "leek-ses-event-destination-routing",
    configuration_set_name=leek_ses_configuration_set.name,
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
            dimension_name=f"leek-{leek_env}",
            value_source="emailHeader",
        )
    ],
)

# Create an Elasticache cluster for Redis caching and Celery broker
redis_config = Config("redis")
redis_cluster_security_group = ec2.SecurityGroup(
    f"leek-redis-cluster-{leek_env}",
    name_prefix=f"leek-redis-{leek_env}-",
    description="Grant access to Redis from Open edX",
    ingress=[
        ec2.SecurityGroupIngressArgs(
            from_port=DEFAULT_REDIS_PORT,
            to_port=DEFAULT_REDIS_PORT,
            protocol="tcp",
            security_groups=[leek_security_group.id],
            description="Allow access from edX to Redis for caching and queueing",
        )
    ],
    tags=aws_config.merged_tags({"Name": f"leek-redis-{leek_env}"}),
    vpc_id=data_vpc["id"],
)

redis_instance_type = (
    redis_config.get("instance_type") or defaults(stack_info)["redis"]["instance_type"]
)
redis_auth_token = leek_secrets["redis"]["token"]
redis_cache_config = OLAmazonRedisConfig(
    encrypt_transit=True,
    auth_token=redis_auth_token,
    cluster_mode_enabled=False,
    encrypted=True,
    engine_version="6.2",
    instance_type=redis_instance_type,
    num_instances=3,
    shard_count=1,
    auto_upgrade=True,
    cluster_description="Redis cluster for edX platform tasks and caching",
    cluster_name=f"leek-redis-{leek_env}",
    security_groups=[redis_cluster_security_group.id],
    subnet_group=data_vpc[
        "elasticache_subnet"
    ],  # the name of the subnet group created in the OLVPC component resource
    tags=aws_config.tags,
)
leek_redis_cache = OLAmazonCache(redis_cache_config)
leek_redis_consul_node = consul.Node(
    "leek-redis-cache-node",
    name="leek-redis",
    address=leek_redis_cache.address,
    opts=consul_provider,
)

leek_redis_consul_service = consul.Service(
    "leek-redis-consul-service",
    node=leek_redis_consul_node.name,
    name="leek-redis",
    port=redis_cache_config.port,
    meta={
        "external-node": True,
        "external-probe": True,
    },
    checks=[
        consul.ServiceCheckArgs(
            check_id="leek-redis",
            interval="10s",
            name="leek-redis",
            timeout="1m0s",
            status="passing",
            tcp=Output.all(
                address=leek_redis_cache.address,
                port=leek_redis_cache.cache_cluster.port,
            ).apply(lambda cluster: "{address}:{port}".format(**cluster)),
        )
    ],
    opts=consul_provider,
)

# Create an auto-scale group for web application servers
leek_web_acm_cert = acm.Certificate(
    "leek-load-balancer-acm-certificate",
    domain_name=leek_domain,
    validation_method="DNS",
    tags=aws_config.tags,
)

leek_acm_cert_validation_records = leek_web_acm_cert.domain_validation_options.apply(
    partial(
        acm_certificate_validation_records,
        zone_id=mitol_zone_id,
        stack_info=stack_info,
    )
)

leek_web_acm_validated_cert = acm.CertificateValidation(
    "wait-for-leek-acm-cert-validation",
    certificate_arn=leek_web_acm_cert.arn,
    validation_record_fqdns=leek_acm_cert_validation_records.apply(
        lambda validation_records: [
            validation_record.fqdn for validation_record in validation_records
        ]
    ),
)
leek_lb_config = OLLoadBalancerConfig(
    subnets=data_vpc["subnet_ids"],
    security_groups=[data_vpc["security_groups"]["web"]],
    tags=aws_config.merged_tags({"Name": f"leek-lb-{stack_info.env_suffix}"}),
    listener_cert_domain=leek_domain,
    listener_cert_arn=leek_web_acm_cert.arn,
)

leek_tg_config = OLTargetGroupConfig(
    vpc_id=data_vpc["id"],
    health_check_interval=60,
    health_check_matcher="200-399",
    health_check_path="/health",
    health_check_unhealthy_threshold=3,  # give extra time for leek to start up
    tags=aws_config.merged_tags({"Name": f"leek-tg-{stack_info.env_suffix}"}),
)

consul_datacenter = consul_stack.require_output("datacenter")
grafana_credentials = read_yaml_secrets(
    Path(f"vector/grafana.{stack_info.env_suffix}.yaml")
)

leek_web_block_device_mappings = [BlockDeviceMapping(volume_size=50)]
leek_web_tag_specs = [
    TagSpecification(
        resource_type="instance",
        tags=aws_config.merged_tags({"Name": f"leek-web-{stack_info.env_suffix}"}),
    ),
    TagSpecification(
        resource_type="volume",
        tags=aws_config.merged_tags({"Name": f"leek-web-{stack_info.env_suffix}"}),
    ),
]

leek_web_lt_config = OLLaunchTemplateConfig(
    block_device_mappings=leek_web_block_device_mappings,
    image_id=leek_ami.id,
    instance_type=leek_config.get("instance_type") or InstanceTypes.burstable_medium,
    instance_profile_arn=leek_profile.arn,
    security_groups=[
        leek_security_group.id,
        consul_security_groups["consul_agent"],
        data_vpc["security_groups"]["web"],
    ],
    tags=aws_config.merged_tags({"Name": f"leek-web-{stack_info.env_suffix}"}),
    tag_specifications=leek_web_tag_specs,
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
                            APPLICATION=leek
                            SERVICE=data-platform
                            VECTOR_CONFIG_DIR=/etc/vector/
                            AWS_REGION={aws_config.region}
                            GRAFANA_CLOUD_API_KEY={grafana_credentials['api_key']}
                            GRAFANA_CLOUD_PROMETHEUS_API_USER={grafana_credentials['prometheus_user_id']}
                            GRAFANA_CLOUD_LOKI_API_USER={grafana_credentials['loki_user_id']}
                            """
                                ),
                                "owner": "root:root",
                            },
                            {
                                "path": "/etc/docker/compose/.env",
                                "content": f"DOMAIN={leek_domain}\nVAULT_ADDR=https://vault-{stack_info.env_suffix}.odl.mit.edu\n",
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

leek_web_auto_scale_config = leek_config.get_object("web_auto_scale") or {
    "desired": 1,
    "min": 1,
    "max": 2,
}
leek_web_asg_config = OLAutoScaleGroupConfig(
    asg_name=f"leek-web-{leek_env}",
    aws_config=aws_config,
    health_check_grace_period=120,
    instance_refresh_warmup=120,
    desired_size=leek_web_auto_scale_config["desired"],
    min_size=leek_web_auto_scale_config["min"],
    max_size=leek_web_auto_scale_config["max"],
    vpc_zone_identifiers=data_vpc["subnet_ids"],
    tags=aws_config.merged_tags({"Name": f"leek-web-{leek_env}"}),
)

leek_web_asg = OLAutoScaling(
    asg_config=leek_web_asg_config,
    lt_config=leek_web_lt_config,
    tg_config=leek_tg_config,
    lb_config=leek_lb_config,
)


# Create an auto-scale group for Celery workers
leek_worker_block_device_mappings = [BlockDeviceMapping(volume_size=50)]
leek_worker_tag_specs = [
    TagSpecification(
        resource_type="instance",
        tags=aws_config.merged_tags({"Name": f"leek-worker-{stack_info.env_suffix}"}),
    ),
    TagSpecification(
        resource_type="volume",
        tags=aws_config.merged_tags({"Name": f"leek-worker-{stack_info.env_suffix}"}),
    ),
]

leek_worker_lt_config = OLLaunchTemplateConfig(
    block_device_mappings=leek_worker_block_device_mappings,
    image_id=leek_ami.id,
    instance_type=leek_config.get("instance_type") or InstanceTypes.burstable_medium,
    instance_profile_arn=leek_profile.arn,
    security_groups=[
        leek_security_group.id,
        consul_security_groups["consul_agent"],
    ],
    tags=aws_config.merged_tags({"Name": f"leek-worker-{stack_info.env_suffix}"}),
    tag_specifications=leek_worker_tag_specs,
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
                            APPLICATION=leek
                            SERVICE=data-platform
                            VECTOR_CONFIG_DIR=/etc/vector/
                            AWS_REGION={aws_config.region}
                            GRAFANA_CLOUD_API_KEY={grafana_credentials['api_key']}
                            GRAFANA_CLOUD_PROMETHEUS_API_USER={grafana_credentials['prometheus_user_id']}
                            GRAFANA_CLOUD_LOKI_API_USER={grafana_credentials['loki_user_id']}
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
                                "content": f"DOMAIN={leek_domain}\nVAULT_ADDR=https://vault-{stack_info.env_suffix}.odl.mit.edu\n",
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

leek_worker_auto_scale_config = leek_config.get_object("worker_auto_scale") or {
    "desired": 1,
    "min": 1,
    "max": 2,
}
leek_worker_asg_config = OLAutoScaleGroupConfig(
    asg_name=f"leek-worker-{leek_env}",
    aws_config=aws_config,
    health_check_type="EC2",
    desired_size=leek_worker_auto_scale_config["desired"],
    min_size=leek_worker_auto_scale_config["min"],
    max_size=leek_worker_auto_scale_config["max"],
    vpc_zone_identifiers=data_vpc["subnet_ids"],
    tags=aws_config.merged_tags({"Name": f"leek-worker-{leek_env}"}),
)

supserset_worker_asg = OLAutoScaling(
    asg_config=leek_worker_asg_config,
    lt_config=leek_worker_lt_config,
)


# Create Route53 DNS records for leek
five_minutes = 60 * 5
route53.Record(
    "leek-server-dns-record",
    name=leek_config.require("domain"),
    type="CNAME",
    ttl=five_minutes,
    records=[leek_web_asg.load_balancer.dns_name],
    zone_id=mitol_zone_id,
    opts=ResourceOptions(delete_before_replace=True),
)
