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
celery_monitoring_config = Config("celery_monitoring")
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
celery_monitoring_env = f"data-{stack_info.env_suffix}"
celery_monitoring_vault_kv_path = vault_mount_stack.require_output(
    "celery_monitoring_kv"
)["path"]
aws_config = AWSBase(tags={"OU": "data", "Environment": celery_monitoring_env})
consul_security_groups = consul_stack.require_output("security_groups")

aws_account = get_caller_identity()
celery_monitoring_domain = celery_monitoring_config.get("domain")
celery_monitoring_mail_domain = f"mail.{celery_monitoring_domain}"
# Create IAM role

celery_monitoring_bucket_name = f"ol-celery-monitoring-{stack_info.env_suffix}"
# Create instance profile for granting access to S3 buckets
celery_monitoring_iam_policy = iam.Policy(
    f"celery-monitoring-policy-{stack_info.env_suffix}",
    name=f"celery-monitoring-policy-{stack_info.env_suffix}",
    path=f"/ol-data/celery-monitoring-policy-{stack_info.env_suffix}/",
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
                            f"arn:aws:s3:::{celery_monitoring_bucket_name}",
                            f"arn:aws:s3:::{celery_monitoring_bucket_name}/*",
                        ],
                    },
                    {
                        "Effect": "Allow",
                        "Action": ["ses:SendEmail", "ses:SendRawEmail"],
                        "Resource": [
                            "arn:*:ses:*:*:identity/*mit.edu",
                            f"arn:aws:ses:*:*:configuration-set/celery-monitoring-{celery_monitoring_env}",
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

celery_monitoring_instance_role = iam.Role(
    "celery-monitoring-instance-role",
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
    name=f"celery-monitoring-instance-role-{stack_info.env_suffix}",
    path="/ol-data/celery-monitoring-role/",
    tags=aws_config.tags,
)

iam.RolePolicyAttachment(
    f"celery-monitoring-role-policy-{stack_info.env_suffix}",
    policy_arn=celery_monitoring_iam_policy.arn,
    role=celery_monitoring_instance_role.name,
)

iam.RolePolicyAttachment(
    f"celery-monitoring-describe-instance-role-policy-{stack_info.env_suffix}",
    policy_arn=policy_stack.require_output("iam_policies")["describe_instances"],
    role=celery_monitoring_instance_role.name,
)

iam.RolePolicyAttachment(
    f"concourse-route53-role-policy-{stack_info.env_suffix}",
    policy_arn=policy_stack.require_output("iam_policies")["route53_ol_zone_records"],
    role=celery_monitoring_instance_role.name,
)

celery_monitoring_profile = iam.InstanceProfile(
    f"celery-monitoring-instance-profile-{stack_info.env_suffix}",
    role=celery_monitoring_instance_role.name,
    name=f"celery-monitoring-instance-profile-{stack_info.env_suffix}",
    path="/ol-data/celery-monitoring-profile/",
)

celery_monitoring_security_group = ec2.SecurityGroup(
    "celery-monitoring-security-group",
    name_prefix=f"celery-monitoring-{celery_monitoring_env}-",
    description="Allow celery_monitoring to connect to Elasticache",
    vpc_id=data_vpc["id"],
    ingress=[],
    egress=[],
    tags=aws_config.merged_tags(
        {"Name": f"celery-monitoring-{celery_monitoring_env}"},
    ),
)

# Get the AMI ID for the celery_monitoring/docker-compose image
celery_monitoring_ami = ec2.get_ami(
    filters=[
        ec2.GetAmiFilterArgs(name="name", values=["celery-monitoring-server-*"]),
        ec2.GetAmiFilterArgs(name="virtualization-type", values=["hvm"]),
        ec2.GetAmiFilterArgs(name="root-device-type", values=["ebs"]),
    ],
    most_recent=True,
    owners=[aws_account.account_id],
)

# Create a vault policy to allow celery_monitoring to get to the secrets it needs
celery_monitoring_server_vault_policy = vault.Policy(
    "celery-monitoring-server-vault-policy",
    name="celery-monitoring-server",
    policy=Path(__file__)
    .parent.joinpath("celery_monitoring_server_policy.hcl")
    .read_text(),
)
# Register celery_monitoring AMI for Vault AWS auth
vault.aws.AuthBackendRole(
    "celery-monitoring-server-ami-ec2-vault-auth",
    backend="aws",
    auth_type="ec2",
    role="celery_monitoring",
    inferred_aws_region=aws_config.region,
    bound_iam_instance_profile_arns=[celery_monitoring_profile.arn],
    bound_ami_ids=[
        celery_monitoring_ami.id
    ],  # Reference the new way of doing stuff, not the old one
    bound_account_ids=[aws_account.account_id],
    bound_vpc_ids=[data_vpc["id"]],
    token_policies=[celery_monitoring_server_vault_policy.name],
    opts=ResourceOptions(delete_before_replace=True),
)

monitored_aws_apps = {
    "odl_video": read_yaml_secrets(
        Path(f"celery_monitoring/data.{stack_info.env_suffix}.yaml")
    ),
    "edxapp-mitxonline": read_yaml_secrets(
        Path(f"celery_monitoring/data.{stack_info.env}")
    ),
}


celery_monitoring_secrets = read_yaml_secrets(
    Path(f"celery_monitoring/data.{stack_info.env_suffix}.yaml")
)
celery_brokers = celery_monitoring_config.get_object("monitored_brokers", [])
celery_monitoring_agent_subscriptions = []
for broker in celery_brokers:
    broker_config = {
        "broker": f"{broker['protocol']}://{[broker['username'] ,broker['password']]}@{broker['host']}:{broker['port']}",  # noqa: E501
        "broker_management_url": "http://mq:15672",
        "backend": None,
        "exchange": "celeryev",
        "queue": "celery_monitoring.fanout",
        "routing_key": "#",
        "org_name": "mono",
        "app_name": "celery_monitoring",
        "app_env": "prod",
        "prefetch_count": 1000,
        "concurrency_pool_size": 2,
        "batch_max_size_in_mb": 1,
        "batch_max_number_of_messages": 1000,
        "batch_max_window_in_seconds": 5,
    }
    celery_monitoring_agent_subscriptions.append(broker_config)
# Actually a single write of the consolidated list object
# That gets written to vault to be read in via consul template from
# the leek bilder project at runtime.
for path, data in celery_monitoring_secrets.items():
    vault.kv.SecretV2(
        f"celery-monitoring-vault-secret-{path}",
        mount=celery_monitoring_vault_kv_path,
        name=path,
        data_json=json.dumps(data),
    )

########################################
# Create SES Service For celery_monitoring Emails #
########################################

celery_monitoring_ses_domain_identity = ses.DomainIdentity(
    "celery-monitoring-ses-domain-identity",
    domain=celery_monitoring_mail_domain,
)
celery_monitoring_ses_verification_record = route53.Record(
    "celery-monitoring-ses-domain-identity-verification-dns-record",
    zone_id=mitol_zone_id,
    name=celery_monitoring_ses_domain_identity.id.apply("_amazonses.{}".format),
    type="TXT",
    ttl=FIVE_MINUTES,
    records=[celery_monitoring_ses_domain_identity.verification_token],
)
celery_monitoring_ses_domain_identity_verification = ses.DomainIdentityVerification(
    "celery-monitoring-ses-domain-identity-verification-resource",
    domain=celery_monitoring_ses_domain_identity.id,
    opts=ResourceOptions(depends_on=[celery_monitoring_ses_verification_record]),
)
celery_monitoring_mail_from_domain = ses.MailFrom(
    "celery-monitoring-ses-mail-from-domain",
    domain=celery_monitoring_ses_domain_identity_verification.domain,
    mail_from_domain=celery_monitoring_ses_domain_identity_verification.domain.apply(
        "bounce.{}".format
    ),
)
celery_monitoring_mail_from_address = ses.EmailIdentity(
    "celery-monitoring-ses-mail-from-identity",
    email=celery_monitoring_config.require("sender_email_address"),
)
# Example Route53 MX record
celery_monitoring_ses_domain_mail_from_mx = route53.Record(
    f"celery-monitoring-ses-mail-from-mx-record-for-{celery_monitoring_env}",
    zone_id=mitol_zone_id,
    name=celery_monitoring_mail_from_domain.mail_from_domain,
    type="MX",
    ttl=FIVE_MINUTES,
    records=["10 feedback-smtp.us-east-1.amazonses.com"],
)
ses_domain_mail_from_txt = route53.Record(
    "celery-monitoring-ses-domain-mail-from-text-record",
    zone_id=mitol_zone_id,
    name=celery_monitoring_mail_from_domain.mail_from_domain,
    type="TXT",
    ttl=FIVE_MINUTES,
    records=["v=spf1 include:amazonses.com -all"],
)
celery_monitoring_ses_domain_dkim = ses.DomainDkim(
    "celery-monitoring-ses-domain-dkim",
    domain=celery_monitoring_ses_domain_identity.domain,
)
for loop_counter in range(3):
    route53.Record(
        f"celery-monitoring-ses-domain-dkim-record-{loop_counter}",
        zone_id=mitol_zone_id,
        name=celery_monitoring_ses_domain_dkim.dkim_tokens[loop_counter].apply(
            lambda dkim_name: f"{dkim_name}._domainkey.{celery_monitoring_mail_domain}"
        ),
        type="CNAME",
        ttl=FIVE_MINUTES,
        records=[
            celery_monitoring_ses_domain_dkim.dkim_tokens[loop_counter].apply(
                "{}.dkim.amazonses.com".format
            )
        ],
    )
celery_monitoring_ses_configuration_set = ses.ConfigurationSet(
    "celery-monitoring-ses-configuration-set",
    reputation_metrics_enabled=True,
    sending_enabled=True,
    name=f"celery-monitoring-{celery_monitoring_env}",
)
celery_monitoring_ses_event_destintations = ses.EventDestination(
    "celery-monitoring-ses-event-destination-routing",
    configuration_set_name=celery_monitoring_ses_configuration_set.name,
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
            dimension_name=f"celery-monitoring-{celery_monitoring_env}",
            value_source="emailHeader",
        )
    ],
)

# Create an Elasticache cluster for Redis caching and Celery broker
redis_config = Config("redis")
redis_cluster_security_group = ec2.SecurityGroup(
    f"celery-monitoring-redis-cluster-{celery_monitoring_env}",
    name_prefix=f"celery-monitoring-redis-{celery_monitoring_env}-",
    description="Grant access to Redis from Open edX",
    ingress=[
        ec2.SecurityGroupIngressArgs(
            from_port=DEFAULT_REDIS_PORT,
            to_port=DEFAULT_REDIS_PORT,
            protocol="tcp",
            security_groups=[celery_monitoring_security_group.id],
            description="Allow access from edX to Redis for caching and queueing",
        )
    ],
    tags=aws_config.merged_tags(
        {"Name": f"celery-monitoring-redis-{celery_monitoring_env}"}
    ),
    vpc_id=data_vpc["id"],
)

redis_instance_type = (
    redis_config.get("instance_type") or defaults(stack_info)["redis"]["instance_type"]
)
redis_auth_token = celery_monitoring_secrets["redis"]["token"]
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
    cluster_name=f"celery-monitoring-redis-{celery_monitoring_env}",
    security_groups=[redis_cluster_security_group.id],
    subnet_group=data_vpc[
        "elasticache_subnet"
    ],  # the name of the subnet group created in the OLVPC component resource
    tags=aws_config.tags,
)
celery_monitoring_redis_cache = OLAmazonCache(redis_cache_config)
celery_monitoring_redis_consul_node = consul.Node(
    "celery-monitoring-redis-cache-node",
    name="celery-monitoring-redis",
    address=celery_monitoring_redis_cache.address,
    opts=consul_provider,
)

celery_monitoring_redis_consul_service = consul.Service(
    "celery-monitoring-redis-consul-service",
    node=celery_monitoring_redis_consul_node.name,
    name="celery-monitoring-redis",
    port=redis_cache_config.port,
    meta={
        "external-node": True,
        "external-probe": True,
    },
    checks=[
        consul.ServiceCheckArgs(
            check_id="celery-monitoring-redis",
            interval="10s",
            name="celery-monitoring-redis",
            timeout="1m0s",
            status="passing",
            tcp=Output.all(
                address=celery_monitoring_redis_cache.address,
                port=celery_monitoring_redis_cache.cache_cluster.port,
            ).apply(lambda cluster: "{address}:{port}".format(**cluster)),
        )
    ],
    opts=consul_provider,
)

# Create an auto-scale group for web application servers
celery_monitoring_web_acm_cert = acm.Certificate(
    "celery-monitoring-load-balancer-acm-certificate",
    domain_name=celery_monitoring_domain,
    validation_method="DNS",
    tags=aws_config.tags,
)

celery_monitoring_acm_cert_validation_records = (
    celery_monitoring_web_acm_cert.domain_validation_options.apply(
        partial(
            acm_certificate_validation_records,
            zone_id=mitol_zone_id,
            stack_info=stack_info,
        )
    )
)

celery_monitoring_web_acm_validated_cert = acm.CertificateValidation(
    "wait-for-celery-monitoring-acm-cert-validation",
    certificate_arn=celery_monitoring_web_acm_cert.arn,
    validation_record_fqdns=celery_monitoring_acm_cert_validation_records.apply(
        lambda validation_records: [
            validation_record.fqdn for validation_record in validation_records
        ]
    ),
)
celery_monitoring_lb_config = OLLoadBalancerConfig(
    subnets=data_vpc["subnet_ids"],
    security_groups=[data_vpc["security_groups"]["web"]],
    tags=aws_config.merged_tags(
        {"Name": f"celery-monitoring-lb-{stack_info.env_suffix}"}
    ),
    listener_cert_domain=celery_monitoring_domain,
    listener_cert_arn=celery_monitoring_web_acm_cert.arn,
)

celery_monitoring_tg_config = OLTargetGroupConfig(
    vpc_id=data_vpc["id"],
    health_check_interval=60,
    health_check_matcher="200-399",
    health_check_path="/health",
    # give extra time for celery_monitoring to start up
    health_check_unhealthy_threshold=3,
    tags=aws_config.merged_tags(
        {"Name": f"celery-monitoring-tg-{stack_info.env_suffix}"}
    ),
)

consul_datacenter = consul_stack.require_output("datacenter")
grafana_credentials = read_yaml_secrets(
    Path(f"vector/grafana.{stack_info.env_suffix}.yaml")
)

celery_monitoring_web_block_device_mappings = [BlockDeviceMapping(volume_size=50)]
celery_monitoring_web_tag_specs = [
    TagSpecification(
        resource_type="instance",
        tags=aws_config.merged_tags(
            {"Name": f"celery-monitoring-web-{stack_info.env_suffix}"}
        ),
    ),
    TagSpecification(
        resource_type="volume",
        tags=aws_config.merged_tags(
            {"Name": f"celery-monitoring-web-{stack_info.env_suffix}"}
        ),
    ),
]

celery_monitoring_web_lt_config = OLLaunchTemplateConfig(
    block_device_mappings=celery_monitoring_web_block_device_mappings,
    image_id=celery_monitoring_ami.id,
    instance_type=celery_monitoring_config.get("instance_type")
    or InstanceTypes.burstable_medium,
    instance_profile_arn=celery_monitoring_profile.arn,
    security_groups=[
        celery_monitoring_security_group.id,
        consul_security_groups["consul_agent"],
        data_vpc["security_groups"]["web"],
    ],
    tags=aws_config.merged_tags(
        {"Name": f"celery-monitoring-web-{stack_info.env_suffix}"}
    ),
    tag_specifications=celery_monitoring_web_tag_specs,
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
                            APPLICATION=celery_monitoring
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
                                "content": f"DOMAIN={celery_monitoring_domain}\nVAULT_ADDR=https://vault-{stack_info.env_suffix}.odl.mit.edu\n",
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

celery_monitoring_web_auto_scale_config = celery_monitoring_config.get_object(
    "web_auto_scale"
) or {
    "desired": 1,
    "min": 1,
    "max": 2,
}
celery_monitoring_web_asg_config = OLAutoScaleGroupConfig(
    asg_name=f"celery-monitoring-web-{celery_monitoring_env}",
    aws_config=aws_config,
    health_check_grace_period=120,
    instance_refresh_warmup=120,
    desired_size=celery_monitoring_web_auto_scale_config["desired"],
    min_size=celery_monitoring_web_auto_scale_config["min"],
    max_size=celery_monitoring_web_auto_scale_config["max"],
    vpc_zone_identifiers=data_vpc["subnet_ids"],
    tags=aws_config.merged_tags(
        {"Name": f"celery-monitoring-web-{celery_monitoring_env}"}
    ),
)

celery_monitoring_web_asg = OLAutoScaling(
    asg_config=celery_monitoring_web_asg_config,
    lt_config=celery_monitoring_web_lt_config,
    tg_config=celery_monitoring_tg_config,
    lb_config=celery_monitoring_lb_config,
)


# Create an auto-scale group for Celery workers
celery_monitoring_worker_block_device_mappings = [BlockDeviceMapping(volume_size=50)]
celery_monitoring_worker_tag_specs = [
    TagSpecification(
        resource_type="instance",
        tags=aws_config.merged_tags(
            {"Name": f"celery-monitoring-worker-{stack_info.env_suffix}"}
        ),
    ),
    TagSpecification(
        resource_type="volume",
        tags=aws_config.merged_tags(
            {"Name": f"celery-monitoring-worker-{stack_info.env_suffix}"}
        ),
    ),
]

celery_monitoring_worker_lt_config = OLLaunchTemplateConfig(
    block_device_mappings=celery_monitoring_worker_block_device_mappings,
    image_id=celery_monitoring_ami.id,
    instance_type=celery_monitoring_config.get("instance_type")
    or InstanceTypes.burstable_medium,
    instance_profile_arn=celery_monitoring_profile.arn,
    security_groups=[
        celery_monitoring_security_group.id,
        consul_security_groups["consul_agent"],
    ],
    tags=aws_config.merged_tags(
        {"Name": f"celery-monitoring-worker-{stack_info.env_suffix}"}
    ),
    tag_specifications=celery_monitoring_worker_tag_specs,
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
                            APPLICATION=celery_monitoring
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
                                "content": f"DOMAIN={celery_monitoring_domain}\nVAULT_ADDR=https://vault-{stack_info.env_suffix}.odl.mit.edu\n",
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

celery_monitoring_worker_auto_scale_config = celery_monitoring_config.get_object(
    "worker_auto_scale"
) or {
    "desired": 1,
    "min": 1,
    "max": 2,
}
celery_monitoring_worker_asg_config = OLAutoScaleGroupConfig(
    asg_name=f"celery-monitoring-worker-{celery_monitoring_env}",
    aws_config=aws_config,
    health_check_type="EC2",
    desired_size=celery_monitoring_worker_auto_scale_config["desired"],
    min_size=celery_monitoring_worker_auto_scale_config["min"],
    max_size=celery_monitoring_worker_auto_scale_config["max"],
    vpc_zone_identifiers=data_vpc["subnet_ids"],
    tags=aws_config.merged_tags(
        {"Name": f"celery-monitoring-worker-{celery_monitoring_env}"}
    ),
)

supserset_worker_asg = OLAutoScaling(
    asg_config=celery_monitoring_worker_asg_config,
    lt_config=celery_monitoring_worker_lt_config,
)


# Create Route53 DNS records for celery_monitoring
five_minutes = 60 * 5
route53.Record(
    "celery-monitoring-server-dns-record",
    name=celery_monitoring_config.require("domain"),
    type="CNAME",
    ttl=five_minutes,
    records=[celery_monitoring_web_asg.load_balancer.dns_name],
    zone_id=mitol_zone_id,
    opts=ResourceOptions(delete_before_replace=True),
)
