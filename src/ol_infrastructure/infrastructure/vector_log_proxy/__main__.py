"""Create the resources needed to run a vector-log-proxy server.  # noqa: D200"""

import base64
import json
import textwrap
from os import linesep
from pathlib import Path

import pulumi_vault as vault
import yaml
from bridge.lib.magic_numbers import DEFAULT_HTTPS_PORT
from bridge.secrets.sops import read_yaml_secrets
from pulumi import Config, Output, ResourceOptions, StackReference, export
from pulumi_aws import acm, autoscaling, ec2, get_caller_identity, iam, lb, route53, s3

from ol_infrastructure.lib.aws.ec2_helper import (
    DiskTypes,
    InstanceTypes,
    default_egress_args,
)
from ol_infrastructure.lib.consul import get_consul_provider
from ol_infrastructure.lib.ol_types import AWSBase
from ol_infrastructure.lib.pulumi_helper import parse_stack
from ol_infrastructure.lib.vault import setup_vault_provider


def build_user_data(consul_dc, challenge_url, service_hash_bucket_fqdn):
    cloud_config_contents = {
        "write_files": [
            {
                "path": "/etc/consul.d/02-autojoin.json",
                "content": json.dumps(
                    {
                        "retry_join": [
                            "provider=aws tag_key=consul_env " f"tag_value={consul_dc}"  # noqa: ISC001, RUF100
                        ],
                        "datacenter": consul_dc,
                    }
                ),
                "owner": "consul:consul",
            },
            {
                "path": "/etc/default/traefik",
                "content": textwrap.dedent(
                    f"""\
            DOMAIN={vector_log_proxy_config.require('web_host_domain')}
            FASTLY_SERVICE_HASH_BUCKET_FQDN={service_hash_bucket_fqdn}
            FASTLY_SERVICE_HASH_BUCKET_CHALLENGE_URL="{challenge_url}"
            """
                ),
                "owner": "root:root",
            },
            {
                "path": "/etc/default/vector",
                "content": textwrap.dedent(
                    f"""\
            ENVIRONMENT={consul_dc}
            APPLICATION=vector-log-proxy
            SERVICE=vector-log-proxy
            VECTOR_CONFIG_DIR=/etc/vector/
            AWS_REGION={aws_config.region}
            GRAFANA_CLOUD_API_KEY={grafana_credentials['api_key']}
            GRAFANA_CLOUD_PROMETHEUS_API_USER={grafana_credentials['prometheus_user_id']}
            GRAFANA_CLOUD_LOKI_API_USER={grafana_credentials['loki_user_id']}
            HEROKU_PROXY_PASSWORD={heroku_proxy_credentials['password']}
            HEROKU_PROXY_USERNAME={heroku_proxy_credentials['username']}
            FASTLY_PROXY_PASSWORD={fastly_proxy_credentials['password']}
            FASTLY_PROXY_USERNAME={fastly_proxy_credentials['username']}
            FASTLY_CHALLENGE_REDIRECT_URL={challenge_url}
            """
                ),
                "owner": "root:root",
            },
        ]
    }

    return base64.b64encode(
        "#cloud-config\n{}".format(
            yaml.dump(
                cloud_config_contents,
                sort_keys=True,
            )
        ).encode("utf8")
    ).decode("utf8")


##################################
##    Setup + Config Retrival   ##
##################################

if Config("vault_server").get("env_namespace"):
    setup_vault_provider()
stack_info = parse_stack()
vector_log_proxy_config = Config("vector_log_proxy")
network_stack = StackReference(f"infrastructure.aws.network.{stack_info.name}")
policy_stack = StackReference("infrastructure.aws.policies")
dns_stack = StackReference("infrastructure.aws.dns")
consul_stack = StackReference(f"infrastructure.consul.operations.{stack_info.name}")
mitodl_zone_id = dns_stack.require_output("odl_zone_id")

env_name = f"{stack_info.env_prefix}-{stack_info.env_suffix}"

target_vpc_name = (
    vector_log_proxy_config.get("target_vpc") or f"{stack_info.env_prefix}_vpc"
)
target_vpc = network_stack.require_output(target_vpc_name)

HEROKU_LOG_PROXY_PORT = vector_log_proxy_config.get("heroku_listener_port") or 9000
FASTLY_LOG_PROXY_PORT = (
    vector_log_proxy_config.get("fastly_listener_port") or DEFAULT_HTTPS_PORT
)

consul_security_groups = consul_stack.require_output("security_groups")
aws_config = AWSBase(
    tags={
        "OU": vector_log_proxy_config.get("business_unit") or "operations",
        "Environment": f"{env_name}",
    }
)
aws_account = get_caller_identity()
vpc_id = target_vpc["id"]
vector_log_proxy_ami = ec2.get_ami(
    filters=[
        ec2.GetAmiFilterArgs(name="name", values=["vector_log_proxy-*"]),
        ec2.GetAmiFilterArgs(name="virtualization-type", values=["hvm"]),
        ec2.GetAmiFilterArgs(name="root-device-type", values=["ebs"]),
    ],
    most_recent=True,
    owners=[aws_account.account_id],
)

vector_log_proxy_tag = f"vector-{env_name}"
consul_provider = get_consul_provider(stack_info)

###############################
##     General Resources     ##
###############################

# IAM and instance profile
vector_log_proxy_instance_role = iam.Role(
    f"vector-log-proxy-instance-role-{env_name}",
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
    path="/ol-infrastructure/vector-web-proxy/role/",
    tags=aws_config.tags,
)
iam.RolePolicyAttachment(
    f"vector-log-proxy-describe-instance-role-policy-{env_name}",
    policy_arn=policy_stack.require_output("iam_policies")["describe_instances"],
    role=vector_log_proxy_instance_role.name,
)
iam.RolePolicyAttachment(
    f"vector-log-proxy-route53-role-policy-{env_name}",
    policy_arn=policy_stack.require_output("iam_policies")["route53_odl_zone_records"],
    role=vector_log_proxy_instance_role.name,
)
vector_log_proxy_instance_profile = iam.InstanceProfile(
    f"vector-log-proxy-instance-profile-{env_name}",
    role=vector_log_proxy_instance_role.name,
    path="/ol-infrastructure/vector-log-proxy/profile/",
)

# Mount Vault secrets backend and populate secrets
vector_log_proxy_secrets_mount = vault.Mount(
    "vector-log-proxy-app-secrets",
    description="Generic secrets storage for vector-log-proxy deployment",
    path="secret-vector-log-proxy",
    type="kv-v2",
)
proxy_credentials = read_yaml_secrets(
    Path(f"vector/vector_log_proxy.{stack_info.env_suffix}.yaml")
)
heroku_proxy_credentials = proxy_credentials["heroku"]
fastly_proxy_credentials = proxy_credentials["fastly"]

# This secret is never actually referenced by anything but it is good to have.
vault.generic.Secret(
    "vector-log-proxy-http-auth-creds",
    path=vector_log_proxy_secrets_mount.path.apply(
        lambda mount_path: f"{mount_path}/basic_auth_credentials"
    ),
    data_json=json.dumps(
        {
            "fastly": {
                "username": fastly_proxy_credentials["username"],
                "password": fastly_proxy_credentials["password"],
            },
            "heroku": {
                "username": heroku_proxy_credentials["username"],
                "password": heroku_proxy_credentials["password"],
            },
        }
    ),
)

# Vault policy definition
vector_log_proxy_vault_policy = vault.Policy(
    "vector-log-proxy-vault-policy",
    name="vector-log-proxy",
    policy=Path(__file__).parent.joinpath("vector_log_proxy_policy.hcl").read_text(),
)
# Register vector-log-proxy AMI for Vault AWS auth
vault.aws.AuthBackendRole(
    "vector-log-proxy-ami-ec2-vault-auth",
    backend="aws",
    auth_type="iam",
    role="vector-log-proxy",
    inferred_entity_type="ec2_instance",
    inferred_aws_region=aws_config.region,
    bound_iam_instance_profile_arns=[vector_log_proxy_instance_profile.arn],
    bound_ami_ids=[vector_log_proxy_ami.id],
    bound_account_ids=[aws_account.account_id],
    bound_vpc_ids=[vpc_id],
    token_policies=[vector_log_proxy_vault_policy.name],
)

##################################
#     Network Access Control     #
##################################
# Create security group
vector_log_proxy_security_group = ec2.SecurityGroup(
    f"vector-log-proxy-security-group-{env_name}",
    name=f"vector-log-proxy-operations-{env_name}",
    description="Access control for vector-log-proxy servers",
    ingress=[
        ec2.SecurityGroupIngressArgs(
            protocol="tcp",
            from_port=HEROKU_LOG_PROXY_PORT,
            to_port=HEROKU_LOG_PROXY_PORT,
            cidr_blocks=["0.0.0.0/0"],
            description=(
                "Allow traffic to the vector-log-proxy server on port"
                f" {HEROKU_LOG_PROXY_PORT}"
            ),
        ),
        ec2.SecurityGroupIngressArgs(
            protocol="tcp",
            from_port=FASTLY_LOG_PROXY_PORT,
            to_port=FASTLY_LOG_PROXY_PORT,
            cidr_blocks=["0.0.0.0/0"],
            description=(
                "Allow traffic to the vector-log-proxy server on port"
                f" {FASTLY_LOG_PROXY_PORT}"
            ),
        ),
    ],
    egress=default_egress_args,
    vpc_id=vpc_id,
)

########################################
#     Fastly Service Hash Challenge    #
########################################
# Need to give fastly a proof-of-ownership for sending logs to a https endpoint
# see:
# https://docs.fastly.com/en/guides/log-streaming-https#prerequisites

fastly_service_hash_bucket_name = f"mitodl-vector-log-proxy-static-{env_name}"
fastly_service_hash_bucket = s3.Bucket(
    f"vector-log-proxy-fastly-service-hash-bucket-{env_name}",
    bucket=fastly_service_hash_bucket_name,
    acl="public-read",
    tags=aws_config.tags,
)

service_hash_content = ""
for service_hash in vector_log_proxy_config.get_object(
    "fastly_service_id_sha256sums"
).values():
    service_hash_content = service_hash_content + service_hash + linesep

fastly_service_hash_bucket_name_with_prefix = ".well-known/fastly/logging/challenge"
fastly_service_hash_object = s3.BucketObjectv2(
    fastly_service_hash_bucket_name_with_prefix,
    bucket=fastly_service_hash_bucket_name,
    acl="public-read",
    content=service_hash_content,
    content_type="text/plain",
    opts=ResourceOptions(parent=fastly_service_hash_bucket),
)

service_hash_bucket_fqdn = fastly_service_hash_bucket.bucket_domain_name.apply(
    lambda domain: f"{domain}"
)
challenge_url = fastly_service_hash_bucket.bucket_domain_name.apply(
    lambda domain: f"https://{domain}/{fastly_service_hash_bucket_name_with_prefix}"
)
export(
    "fastly_service_hash_bucket",
    {"challenge_url": challenge_url, "bucket_fqdn": service_hash_bucket_fqdn},
)

###################################
#     Web Node EC2 Deployment     #
###################################

# Create load balancer for Concourse web nodes
LOAD_BALANCER_NAME_MAX_LENGTH = 32
vector_log_proxy_lb = lb.LoadBalancer(
    "vector-log-proxy-load-balancer",
    name=f"vector-log-proxy-alb-{stack_info.env_prefix[:3]}-{stack_info.env_suffix[:2]}"[
        :LOAD_BALANCER_NAME_MAX_LENGTH
    ],
    ip_address_type="dualstack",
    load_balancer_type="application",
    enable_http2=True,
    subnets=target_vpc["subnet_ids"],
    security_groups=[
        vector_log_proxy_security_group.id,
    ],
    tags=aws_config.merged_tags({"Name": vector_log_proxy_tag}),
)

TARGET_GROUP_NAME_MAX_LENGTH = 32

heroku_log_proxy_lb_target_group = lb.TargetGroup(
    "heroku-vector-log-proxy-alb-target-group",
    vpc_id=vpc_id,
    target_type="instance",
    port=HEROKU_LOG_PROXY_PORT,
    protocol="HTTPS",
    health_check=lb.TargetGroupHealthCheckArgs(
        healthy_threshold=2,
        interval=120,
        path="/",
        port=str(HEROKU_LOG_PROXY_PORT),
        protocol="HTTPS",
        matcher="405",
    ),
    name=("heroku-" + vector_log_proxy_tag)[:TARGET_GROUP_NAME_MAX_LENGTH].rstrip("-"),
    tags=aws_config.tags,
)
fastly_log_proxy_lb_target_group = lb.TargetGroup(
    "fastly-vector-log-proxy-alb-target-group",
    vpc_id=vpc_id,
    target_type="instance",
    port=FASTLY_LOG_PROXY_PORT,
    protocol="HTTPS",
    health_check=lb.TargetGroupHealthCheckArgs(
        healthy_threshold=2,
        interval=120,
        path="/",
        port=str(FASTLY_LOG_PROXY_PORT),
        protocol="HTTPS",
        matcher="405",
    ),
    name=("fastly-" + vector_log_proxy_tag)[:TARGET_GROUP_NAME_MAX_LENGTH].rstrip("-"),
    tags=aws_config.tags,
)

vector_log_proxy_acm_cert = acm.get_certificate(
    domain="*.odl.mit.edu", most_recent=True, statuses=["ISSUED"]
)
heroku_log_proxy_alb_listener = lb.Listener(
    "heroku-vector-log-proxy-alb-listener",
    certificate_arn=vector_log_proxy_acm_cert.arn,
    load_balancer_arn=vector_log_proxy_lb.arn,
    port=HEROKU_LOG_PROXY_PORT,
    protocol="HTTPS",
    default_actions=[
        lb.ListenerDefaultActionArgs(
            type="forward",
            target_group_arn=heroku_log_proxy_lb_target_group.arn,
        )
    ],
)
heroku_log_proxy_alb_listener = lb.Listener(
    "fastly-vector-log-proxy-alb-listener",
    certificate_arn=vector_log_proxy_acm_cert.arn,
    load_balancer_arn=vector_log_proxy_lb.arn,
    port=FASTLY_LOG_PROXY_PORT,
    protocol="HTTPS",
    default_actions=[
        lb.ListenerDefaultActionArgs(
            type="forward",
            target_group_arn=fastly_log_proxy_lb_target_group.arn,
        )
    ],
)

## Create auto scale group and launch configs for vector-log-proxy
instance_type = (
    vector_log_proxy_config.get("instance_type") or InstanceTypes.burstable_small.name
)
consul_datacenter = consul_stack.require_output("datacenter")

grafana_credentials = read_yaml_secrets(
    Path(f"vector/grafana.{stack_info.env_suffix}.yaml")
)

vector_log_proxy_launch_config = ec2.LaunchTemplate(
    "vector-log-proxy-launch-template",
    name_prefix=f"vector-server-{env_name}-",
    description="Launch template for deploying vector-log-proxy nodes",
    iam_instance_profile=ec2.LaunchTemplateIamInstanceProfileArgs(
        arn=vector_log_proxy_instance_profile.arn,
    ),
    image_id=vector_log_proxy_ami.id,
    block_device_mappings=[
        ec2.LaunchTemplateBlockDeviceMappingArgs(
            device_name="/dev/xvda",
            ebs=ec2.LaunchTemplateBlockDeviceMappingEbsArgs(
                volume_size=vector_log_proxy_config.get_int("disk_size") or 25,
                volume_type=DiskTypes.ssd,
                delete_on_termination=True,
            ),
        )
    ],
    vpc_security_group_ids=[
        vector_log_proxy_security_group.id,
        consul_security_groups["consul_agent"],
    ],
    instance_type=InstanceTypes[instance_type].value,
    key_name="oldevops",
    tag_specifications=[
        ec2.LaunchTemplateTagSpecificationArgs(
            resource_type="instance",
            tags=aws_config.merged_tags({"Name": vector_log_proxy_tag}),
        ),
        ec2.LaunchTemplateTagSpecificationArgs(
            resource_type="volume",
            tags=aws_config.merged_tags({"Name": vector_log_proxy_tag}),
        ),
    ],
    tags=aws_config.tags,
    user_data=Output.all(
        consul_dc=consul_datacenter,
        challenge_url=challenge_url,
        service_hash_bucket_fqdn=service_hash_bucket_fqdn,
    ).apply(
        lambda init_dict: build_user_data(
            init_dict["consul_dc"],
            init_dict["challenge_url"],
            init_dict["service_hash_bucket_fqdn"],
        )
    ),
)

autoscaling.Group(
    "vector-log-proxy-autoscaling-group",
    desired_capacity=2,
    min_size=2,
    max_size=3,
    health_check_type="ELB",
    vpc_zone_identifiers=target_vpc["subnet_ids"],
    launch_template=autoscaling.GroupLaunchTemplateArgs(
        id=vector_log_proxy_launch_config.id, version="$Latest"
    ),
    instance_refresh=autoscaling.GroupInstanceRefreshArgs(
        strategy="Rolling",
        preferences=autoscaling.GroupInstanceRefreshPreferencesArgs(
            min_healthy_percentage=50
        ),
        triggers=["tag"],
    ),
    target_group_arns=[
        heroku_log_proxy_lb_target_group.arn,
        fastly_log_proxy_lb_target_group.arn,
    ],
    tags=[
        autoscaling.GroupTagArgs(
            key=key_name,
            value=key_value,
            propagate_at_launch=True,
        )
        for key_name, key_value in aws_config.merged_tags(
            {"ami_id": vector_log_proxy_ami.id}
        ).items()
    ],
)

## Create Route53 DNS records for vector-log-proxy nodes
five_minutes = 60 * 5
dns_entry = route53.Record(
    "vector-log-proxy-dns-record",
    name=vector_log_proxy_config.require("web_host_domain"),
    type="CNAME",
    ttl=five_minutes,
    records=[vector_log_proxy_lb.dns_name],
    zone_id=mitodl_zone_id,
)

export("vector_log_proxy", {"fqdn": dns_entry.fqdn})
