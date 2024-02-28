import base64
import json
import textwrap
from pathlib import Path
from pprint import pformat

import pulumi_vault as vault
import yaml
from bridge.secrets.sops import read_yaml_secrets
from pulumi import Config, Output, ResourceOptions, StackReference, log
from pulumi_aws import acm, ec2, get_caller_identity, iam, route53

from ol_infrastructure.components.aws.auto_scale_group import (
    BlockDeviceMapping,
    OLAutoScaleGroupConfig,
    OLAutoScaling,
    OLLaunchTemplateConfig,
    OLLoadBalancerConfig,
    OLTargetGroupConfig,
    TagSpecification,
)
from ol_infrastructure.lib.aws.ec2_helper import InstanceTypes
from ol_infrastructure.lib.consul import get_consul_provider
from ol_infrastructure.lib.ol_types import AWSBase
from ol_infrastructure.lib.pulumi_helper import parse_stack
from ol_infrastructure.lib.vault import setup_vault_provider

setup_vault_provider()
celery_monitoring_config = Config("celery_monitoring")
stack_info = parse_stack()
consul_provider = get_consul_provider(stack_info)
network_stack = StackReference(f"infrastructure.aws.network.{stack_info.name}")
dns_stack = StackReference("infrastructure.aws.dns")
consul_stack = StackReference(f"infrastructure.consul.operations.{stack_info.name}")
opensearch_stack = StackReference(
    f"infrastructure.aws.opensearch.celery_monitoring.{stack_info.name}"
)
vault_infra_stack = StackReference(f"infrastructure.vault.operations.{stack_info.name}")
vault_mount_stack = StackReference(
    f"substructure.vault.static_mounts.operations.{stack_info.name}"
)

policy_stack = StackReference("infrastructure.aws.policies")


def build_broker_subscriptions(
    edx_outputs: list[Output],
) -> str:
    """Create a dict of Redis cache configs for each edxapp stack"""
    broker_subs = []
    for edx_output in edx_outputs:
        broker_subs.append(  # noqa: PERF401
            {
                "broker": edx_output["redis"],
                "broker_management_url": "http://mq:15672",
                # Does this belong here?
                "broker_redis_token": edx_output["redis_token"],
                "backend": None,
                "exchange": "celeryev",  # Does this want to be the exchange of the leek
                # OpenSearch? I'd think we'd want the edxapp one?
                "queue": "celery_monitoring.fanout",
                "routing_key": "#",
                "org_name": "mono",
                "prefetch_count": 1000,
                "concurrency_pool_size": 2,
                "batch_max_size_in_mb": 1,
                "batch_max_number_of_messages": 1000,
                "batch_max_window_in_seconds": 5,
            }
        )
    log.info(f"build_broker_subscrpitons: broker_sub={pformat(broker_subs)}")
    arbitrary_dict = {"broker_subscriptions": broker_subs}
    return json.dumps(arbitrary_dict)


stacks = [
    f"applications.edxapp.xpro.{stack_info.name}",
    f"applications.edxapp.mitx.{stack_info.name}",
    f"applications.edxapp.mitx-staging.{stack_info.name}",
]
# mitxonline CI is not up at the moment.
if stack_info.name != "CI":
    stacks.append(
        f"applications.edxapp.mitxonline.{stack_info.name}",
    )

redis_outputs: list[Output] = []
for stack in stacks:
    redis_outputs.append(StackReference(stack).require_output("edxapp"))  # noqa: PERF401
redis_broker_subscriptions = Output.all(*redis_outputs).apply(
    build_broker_subscriptions
)

celery_monitoring_vault_kv_path = vault_mount_stack.require_output(
    "celery_monitoring_kv"
)["path"]

vault.kv.SecretV2(
    "celery-monitoring-vault-secret-redis-brokers",
    mount=celery_monitoring_vault_kv_path,
    name="redis_brokers",
    data_json=redis_broker_subscriptions,
)

mitol_zone_id = dns_stack.require_output("ol")["id"]
operations_vpc = network_stack.require_output("operations_vpc")
celery_monitoring_env = f"operations-{stack_info.env_suffix}"
aws_config = AWSBase(tags={"OU": "operations", "Environment": celery_monitoring_env})
consul_security_groups = consul_stack.require_output("security_groups")

aws_account = get_caller_identity()
celery_monitoring_domain = celery_monitoring_config.get("web_host_domain")
celery_monitoring_mail_domain = f"mail.{celery_monitoring_domain}"

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
    path="/ol-operations/celery-monitoring-role/",
    tags=aws_config.tags,
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
    path="/ol-operations/celery-monitoring-profile/",
)

celery_monitoring_security_group = ec2.SecurityGroup(
    "celery-monitoring-security-group",
    name_prefix=f"celery-monitoring-{celery_monitoring_env}-",
    description="Allow celery_monitoring to connect to Elasticache",
    vpc_id=operations_vpc["id"],
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
    bound_vpc_ids=[operations_vpc["id"]],
    token_policies=[celery_monitoring_server_vault_policy.name],
    opts=ResourceOptions(delete_before_replace=True),
)


# ACM lookup of ODL's wildcardcert
celery_monitoring_web_acm_cert = acm.get_certificate(
    domain="*.odl.mit.edu", most_recent=True, statuses=["ISSUED"]
)
celery_monitoring_lb_config = OLLoadBalancerConfig(
    subnets=operations_vpc["subnet_ids"],
    security_groups=[operations_vpc["security_groups"]["web"]],
    tags=aws_config.merged_tags(
        {"Name": f"celery-monitoring-lb-{stack_info.env_suffix}"}
    ),
    listener_cert_domain=celery_monitoring_domain,
    listener_cert_arn=celery_monitoring_web_acm_cert.arn,
)

celery_monitoring_tg_config = OLTargetGroupConfig(
    vpc_id=operations_vpc["id"],
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
        operations_vpc["security_groups"]["web"],
        operations_vpc["security_groups"]["celery_monitoring"],
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
                            SERVICE=celery-monitoring
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
                                "content": textwrap.dedent(
                                    f"""\
                                DOMAIN={celery_monitoring_domain}
                                VAULT_ADDR=https://vault-{stack_info.env_suffix}.odl.mit.edu
                                LEEK_API_LOG_LEVEL=WARNING
                                LEEK_AGENT_LOG_LEVEL=INFO
                                LEEK_ENABLE_API=true
                                LEEK_ENABLE_AGENT=true
                                LEEK_ENABLE_WEB=true
                                LEEK_API_URL=http://0.0.0.0:5000
                                LEEK_WEB_URL=http://0.0.0.0:8000
                                LEEK_ES_URL=http://es01:9200
                                LEEK_API_ENABLE_AUTH=false
                                LEEK_AGENT_SUBSCRIPTIONS={redis_broker_subscriptions}
                                LEEK_AGENT_API_SECRET=not-secret
                                """
                                ),
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
    vpc_zone_identifiers=operations_vpc["subnet_ids"],
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
