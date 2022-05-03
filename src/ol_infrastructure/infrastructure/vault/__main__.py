"""Deploy a cluster of Vault servers using autoscale groups and KMS auto-unseal.

- Creates an instance policy granting access to IAM for use with the AWS secrets
  backend, and granting permissions to a KMS key for auto-unseal.

- Creates an autoscale group that launches a pre-built AMI with Vault installed.

- Creates a load balancer and attaches it to the ASG with an internal Route53 entry
  for simplifying discovery of the Vault cluster.

- Uses the cloud auto-join functionality to automate new instances joining the
  cluster.  The requisite configuration is passed in via cloud-init user data.
"""
import base64
import json
import textwrap
from pathlib import Path
from typing import Any, Dict

import pulumi_tls as tls
import yaml
from pulumi import Config, Output, ResourceOptions, StackReference, export
from pulumi_aws import (
    acm,
    acmpca,
    autoscaling,
    ec2,
    get_caller_identity,
    iam,
    lb,
    route53,
)

from bridge.lib.magic_numbers import (
    DEFAULT_HTTPS_PORT,
    DEFAULT_RSA_KEY_SIZE,
    FIVE_MINUTES,
    IAM_ROLE_NAME_PREFIX_MAX_LENGTH,
    VAULT_CLUSTER_PORT,
    VAULT_HTTP_PORT,
)
from bridge.secrets.sops import read_yaml_secrets
from ol_infrastructure.lib.aws.ec2_helper import DiskTypes, InstanceTypes
from ol_infrastructure.lib.aws.iam_helper import IAM_POLICY_VERSION, lint_iam_policy
from ol_infrastructure.lib.ol_types import AWSBase
from ol_infrastructure.lib.pulumi_helper import parse_stack

###############
# Stack Setup #
###############
vault_config = Config("vault")
stack_info = parse_stack()
target_network = vault_config.require("target_vpc")
ca_stack = StackReference("infrastructure.aws.private_ca")
consul_stack = StackReference(
    f"infrastructure.consul.{stack_info.env_prefix}.{stack_info.name}"
)
dns_stack = StackReference("infrastructure.aws.dns")
kms_stack = StackReference(f"infrastructure.aws.kms.{stack_info.name}")
network_stack = StackReference(f"infrastructure.aws.network.{stack_info.name}")
policy_stack = StackReference("infrastructure.aws.policies")

##################
# Variable Setup #
##################
env_name = f"{stack_info.env_prefix}-{stack_info.env_suffix}"
aws_config = AWSBase(
    tags={
        "OU": vault_config.get("business_unit") or "operations",
        "Environment": env_name,
        "Owner": "platform-engineering",
    }
)
aws_account = get_caller_identity()
kms_ebs = kms_stack.require_output("kms_ec2_ebs_key")
odl_zone_id = dns_stack.require_output("odl_zone_id")
root_ca = ca_stack.require_output("root_ca")
target_vpc = network_stack.require_output(f"{target_network}_vpc")
vault_domain = vault_config.require("domain")
vault_unseal_key = kms_stack.require_output("vault_auto_unseal_key")
vault_ami = ec2.get_ami(
    filters=[
        ec2.GetAmiFilterArgs(name="name", values=["vault-server-*"]),
        ec2.GetAmiFilterArgs(name="virtualization-type", values=["hvm"]),
        ec2.GetAmiFilterArgs(name="root-device-type", values=["ebs"]),
    ],
    most_recent=True,
    owners=[aws_account.account_id],
)

#######################
# Access and Security #
#######################


def vault_policy_document(vault_key_arn) -> Dict[str, Any]:
    return {
        "Version": IAM_POLICY_VERSION,
        "Statement": [
            {
                "Effect": "Allow",
                "Action": [
                    "kms:Encrypt",
                    "kms:Decrypt",
                    "kms:DescribeKey",
                ],
                "Resource": vault_key_arn,
            },
            {
                "Effect": "Allow",
                "Action": [
                    "iam:AttachUserPolicy",
                    "iam:CreateAccessKey",
                    "iam:CreateUser",
                    "iam:DeleteAccessKey",
                    "iam:DeleteUser",
                    "iam:DeleteUserPolicy",
                    "iam:DetachUserPolicy",
                    "iam:ListAccessKeys",
                    "iam:ListAttachedUserPolicies",
                    "iam:ListGroupsForUser",
                    "iam:ListUserPolicies",
                    "iam:PutUserPolicy",
                    "iam:AddUserToGroup",
                    "iam:RemoveUserFromGroup",
                ],
                "Resource": ["arn:*:iam::*:user/vault-*", "arn:*:iam::*:group/*"],
            },
            {
                "Effect": "Allow",
                "Action": [
                    "iam:GetRole",
                ],
                "Resource": ["arn:*:iam::*:role/*"],
            },
        ],
    }


parliament_config = {
    "CREDENTIALS_EXPOSURE": {
        "ignore_locations": [{"actions": ["iam:createaccesskey"]}]
    },
    "PRIVILEGE_ESCALATION": {
        "ignore_locations": [
            {"type": "CreateAccessKey", "actions": ["iam:createaccesskey"]},
            {
                "type": "AttachUserPolicy",
                "actions": ["iam:attachuserpolicy"],
            },
            {
                "type": "PutUserPolicy",
                "actions": ["iam:putuserpolicy"],
            },
            {"type": "AddUserToGroup", "actions": ["iam:addusertogroup"]},
        ]
    },
    "PERMISSIONS_MANAGEMENT_ACTIONS": {
        "ignore_locations": [
            {
                "actions": [
                    "iam:deleteuserpolicy",
                    "iam:deleteuser",
                    "iam:attachuserpolicy",
                    "iam:putuserpolicy",
                    "iam:createaccesskey",
                    "iam:deleteaccesskey",
                    "iam:detachuserpolicy",
                    "iam:createuser",
                ]
            }
        ]
    },
    "RESOURCE_EFFECTIVELY_STAR": {"ignore_locations": []},
}

# IAM Policy and role
vault_policy = iam.Policy(
    "vault-policy",
    name_prefix="vault-server-policy-",
    path=f"/ol-applications/vault/{stack_info.env_prefix}/{stack_info.env_suffix}/",
    policy=vault_unseal_key["arn"].apply(
        lambda arn: lint_iam_policy(
            vault_policy_document(arn),
            stringify=True,
            parliament_config=parliament_config,
        )
    ),
    description="AWS access permissions for Vault server instances",
)
vault_iam_role = iam.Role(
    "vault-instance-role",
    assume_role_policy=json.dumps(
        {
            "Version": IAM_POLICY_VERSION,
            "Statement": {
                "Effect": "Allow",
                "Action": "sts:AssumeRole",
                "Principal": {"Service": "ec2.amazonaws.com"},
            },
        }
    ),
    name_prefix=f"{env_name}-vault-server-role-"[:IAM_ROLE_NAME_PREFIX_MAX_LENGTH],
    path=f"/ol-applications/vault/{stack_info.env_prefix}/{stack_info.env_suffix}/",
    tags=aws_config.tags,
)
iam.RolePolicyAttachment(
    "vault-describe-instances-permission",
    policy_arn=policy_stack.require_output("iam_policies")["describe_instances"],
    role=vault_iam_role.name,
)
iam.RolePolicyAttachment(
    "caddy-route53-records-permission",
    policy_arn=policy_stack.require_output("iam_policies")["route53_odl_zone_records"],
    role=vault_iam_role.name,
)
iam.RolePolicyAttachment(
    "vault-role-policy",
    policy_arn=vault_policy.arn,
    role=vault_iam_role.name,
)
vault_instance_profile = iam.InstanceProfile(
    f"vault-server-instance-profile-{env_name}",
    name_prefix=f"{env_name}-vault-server-",
    role=vault_iam_role.name,
    path=f"/ol-applications/vault/{stack_info.env_prefix}/{stack_info.env_suffix}/",
)

# Security Group
vault_security_group = ec2.SecurityGroup(
    "vault-server-security-group",
    name_prefix=f"vault-server-{env_name}-",
    description="Network access controls for traffic to and from Vault servers",
    ingress=[
        ec2.SecurityGroupIngressArgs(
            protocol="tcp",
            from_port=VAULT_HTTP_PORT,
            to_port=VAULT_CLUSTER_PORT,
            self=True,
            description="Allow traffic between Vault server nodes in a cluster",
        ),
        ec2.SecurityGroupIngressArgs(
            protocol="tcp",
            from_port=VAULT_HTTP_PORT,
            to_port=VAULT_HTTP_PORT,
            cidr_blocks=["0.0.0.0/0"],
            description="Allow traffic to Vault server API endpoints",
        ),
    ],
    tags=aws_config.merged_tags({"Name": f"vault-server-{env_name}"}),
    vpc_id=target_vpc["id"],
)

#################
# Load Balancer #
#################
# Create load balancer for Edxapp web nodes
web_lb = lb.LoadBalancer(
    "vault-web-load-balancer",
    name_prefix=f"vault-server-{env_name}-"[:6],
    ip_address_type="dualstack",
    load_balancer_type="application",
    enable_http2=True,
    subnets=target_vpc["subnet_ids"],
    security_groups=[
        target_vpc["security_groups"]["web"],
    ],
    tags=aws_config.merged_tags({"Name": f"vault-server-{env_name}"}),
)

TARGET_GROUP_NAME_MAX_LENGTH = 32
vault_web_lb_target_group = lb.TargetGroup(
    "vault-web-alb-target-group",
    vpc_id=target_vpc["id"],
    target_type="instance",
    port=DEFAULT_HTTPS_PORT,
    protocol="HTTPS",
    health_check=lb.TargetGroupHealthCheckArgs(
        healthy_threshold=3,
        timeout=3,
        interval=10,
        path="/v1/sys/health?uninitcode=499",
        port=str(DEFAULT_HTTPS_PORT),
        protocol="HTTPS",
        matcher="200,429,499",
    ),
    name_prefix=f"vault-{stack_info.env_suffix}-"[:6],
    tags=aws_config.tags,
)
odl_wildcard_cert = acm.get_certificate(
    domain="*.odl.mit.edu", most_recent=True, statuses=["ISSUED"]
)
vault_web_alb_listener = lb.Listener(
    "vault-web-alb-listener",
    certificate_arn=odl_wildcard_cert.arn,
    load_balancer_arn=web_lb.arn,
    port=DEFAULT_HTTPS_PORT,
    protocol="HTTPS",
    default_actions=[
        lb.ListenerDefaultActionArgs(
            type="forward",
            target_group_arn=vault_web_lb_target_group.arn,
        )
    ],
    opts=ResourceOptions(delete_before_replace=True),
)

##########################
# Certificate Management #
##########################
vault_listener_key = tls.PrivateKey(
    "vault-server-listener-tls-key",
    algorithm="RSA",
    rsa_bits=DEFAULT_RSA_KEY_SIZE,
)
vault_listener_csr = tls.CertRequest(
    "vault-server-lisetner-tls-cert-request",
    dns_names=[
        "active.vault.service.consul",
        "vault.service.consul",
        "vault.query.consul",
    ],
    key_algorithm=vault_listener_key.algorithm,
    private_key_pem=vault_listener_key.private_key_pem,
    subjects=[
        tls.CertRequestSubjectArgs(
            country="US",
            province="Massachusetts",
            locality="Cambridge",
            organization="Massachusetts Institute of Technology",
            organizational_unit="Open Learning",
            common_name=vault_domain,
        )
    ],
)
vault_listener_cert = acmpca.Certificate(
    "vault-server-listener-tls-certificate",
    certificate_authority_arn=root_ca["arn"],
    certificate_signing_request=vault_listener_csr.cert_request_pem,
    signing_algorithm="SHA512WITHRSA",
    validity=acmpca.CertificateValidityArgs(type="YEARS", value="1"),
)

###################
# Autoscale Group #
###################


def cloud_init_user_data(
    kms_key_id,
    vpc_id,
    consul_env_name,
    vault_dns_name,
    tls_key,
    tls_cert,
    ca_cert,
) -> str:
    grafana_credentials = read_yaml_secrets(
        Path(f"vector/grafana.{stack_info.env_suffix}.yaml")
    )
    cloud_config_contents = {
        ""
        "write_files": [
            {
                "path": "/etc/consul.d/99-autojoin.json",
                "content": json.dumps(
                    {
                        "retry_join": [
                            "provider=aws tag_key=consul_env "
                            f"tag_value={consul_env_name}"
                        ],
                        "datacenter": consul_env_name,
                    }
                ),
                "owner": "consul:consul",
            },
            {
                "path": "/etc/default/caddy",
                "content": (f"DOMAIN={vault_dns_name}\n"),
            },
            {
                "path": "/var/opt/kms_key_id",
                "content": kms_key_id,
            },
            {
                "path": "/etc/vault/zz-autojoin.json",
                "content": json.dumps(
                    {
                        "storage": {
                            "raft": {
                                "retry_join": [
                                    {
                                        "auto_join": (
                                            "provider=aws "
                                            "tag_key=vault_env "
                                            f"tag_value={vpc_id}"
                                        ),
                                        "auto_join_port": VAULT_HTTP_PORT,
                                        "leader_tls_servername": "active.vault.service.consul",  # noqa: E501
                                        "leader_ca_cert_file": "/etc/ssl/ol_root_ca.pem",  # noqa: E501
                                    }
                                ],
                                "performance_multiplier": 5,
                                "path": "/var/lib/vault/raft/",
                            }
                        }
                    }
                ),
            },
            {
                "path": "/etc/default/vector",
                "content": textwrap.dedent(
                    f"""\
                    ENVIRONMENT={consul_env_name}
                    VECTOR_CONFIG_DIR=/etc/vector/
                    GRAFANA_CLOUD_API_KEY={grafana_credentials['api_key']}
                    GRAFANA_CLOUD_PROMETHEUS_API_USER={grafana_credentials['prometheus_user_id']}
                    GRAFANA_CLOUD_LOKI_API_USER={grafana_credentials['loki_user_id']}
                    """
                ),  # noqa: WPS355
                "owner": "root:root",
            },
            # TODO: Move TLS key and cert injection to Packer build so that private key
            # information isn't being passed as userdata (TMM 2021-08-06)
            {
                "path": "/etc/vault/ssl/vault.key",
                "content": tls_key,
                "permissions": "0400",
                "owner": "vault:vault",
            },
            {
                "path": "/etc/vault/ssl/vault.cert",
                "content": tls_cert,
                "permissions": "0400",
                "owner": "vault:vault",
            },
            {"path": "/etc/ssl/ol_root_ca.pem", "content": ca_cert},
        ],
    }
    cloud_config = "#cloud-config\n{}".format(
        yaml.dump(
            cloud_config_contents,
            sort_keys=False,
        )
    ).encode("utf8")
    return base64.b64encode(cloud_config).decode("utf8")


cloud_init_param = Output.all(
    vpc_id=target_vpc["id"],
    key_id=vault_unseal_key["id"],
    tls_key=vault_listener_key.private_key_pem,
    tls_cert=vault_listener_cert.certificate,
    ca_cert=root_ca["certificate"],
).apply(
    lambda init_inputs: cloud_init_user_data(
        init_inputs["key_id"],
        init_inputs["vpc_id"],
        env_name,
        vault_domain,
        init_inputs["tls_key"],
        init_inputs["tls_cert"],
        init_inputs["ca_cert"],
    )
)

vault_instance_type = (
    vault_config.get("instance_type") or InstanceTypes.general_purpose_intel_xlarge.name
)
vault_launch_config = ec2.LaunchTemplate(
    "vault-server-launch-template",
    name_prefix=f"vault-{env_name}-",
    description="Launch template for deploying Vault server nodes",
    iam_instance_profile=ec2.LaunchTemplateIamInstanceProfileArgs(
        arn=vault_instance_profile.arn,
    ),
    image_id=vault_ami.id,
    user_data=cloud_init_param,
    block_device_mappings=[
        ec2.LaunchTemplateBlockDeviceMappingArgs(
            device_name=vault_ami.root_device_name,
            ebs=ec2.LaunchTemplateBlockDeviceMappingEbsArgs(
                volume_size=vault_config.get_int("storage_disk_capacity") or 100,
                volume_type=DiskTypes.ssd,
                delete_on_termination=True,
                encrypted=True,
                kms_key_id=kms_ebs["arn"],
            ),
        ),
    ],
    vpc_security_group_ids=[
        vault_security_group.id,
        target_vpc["security_groups"]["web"],
        consul_stack.require_output("security_groups")["consul_agent"],
    ],
    instance_type=InstanceTypes[vault_instance_type].value,
    key_name="oldevops",
    tag_specifications=[
        ec2.LaunchTemplateTagSpecificationArgs(
            resource_type="instance",
            tags=aws_config.merged_tags(
                {"Name": f"vault-server-{env_name}", "vault_env": target_vpc["id"]}
            ),
        ),
        ec2.LaunchTemplateTagSpecificationArgs(
            resource_type="volume",
            tags=aws_config.merged_tags(
                {"Name": f"vault-server-{env_name}", "vault_env": target_vpc["id"]}
            ),
        ),
    ],
    tags=aws_config.tags,
)
# Valid values are 3, 5, or 7
cluster_count = vault_config.get_int("cluster_size") or 3
vault_asg = autoscaling.Group(
    "vault-server-autoscaling-group",
    desired_capacity=cluster_count,
    min_size=cluster_count,
    max_size=cluster_count,
    health_check_type="EC2",
    vpc_zone_identifiers=target_vpc["subnet_ids"],
    launch_template=autoscaling.GroupLaunchTemplateArgs(
        id=vault_launch_config.id, version="$Latest"
    ),
    instance_refresh=autoscaling.GroupInstanceRefreshArgs(
        strategy="Rolling",
        preferences=autoscaling.GroupInstanceRefreshPreferencesArgs(
            min_healthy_percentage=90,  # noqa: WPS432
            instance_warmup=FIVE_MINUTES * 3,
        ),
        triggers=["tag"],
    ),
    target_group_arns=[vault_web_lb_target_group.arn],
    tags=[
        autoscaling.GroupTagArgs(
            key=key_name,
            value=key_value,
            propagate_at_launch=True,
        )
        for key_name, key_value in aws_config.merged_tags(
            {"ami_id": vault_ami.id}
        ).items()
    ],
)

vault_public_dns = route53.Record(
    "vault-server-dns-record",
    name=vault_config.require("domain"),
    type="CNAME",
    ttl=FIVE_MINUTES,
    records=[web_lb.dns_name],
    zone_id=odl_zone_id,
)
#################
# Stack Exports #
#################
export(
    "vault_server",
    {
        "security_group": vault_security_group.id,
        "public_dns": vault_public_dns.fqdn,
        "cluster_address": vault_public_dns.fqdn.apply("https://{}".format),
        "environment_namespace": f"{stack_info.env_prefix}.{stack_info.env_suffix}",
    },
)
