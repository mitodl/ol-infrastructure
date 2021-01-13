"""Provision an instance of the OCW build machine.

This will run the ocw-to-hugo and hugo-course-publisher applications, and will push web
content to the OCW site's S3 buckets.

    - Ensure the correct state of the destination web bucket

    - Ensure the correct state of the IAM instance role for the EC2 instance to have
      access to the input and output S3 buckets.

    - Register a minion ID and key pair with the appropriate SaltStack master instance

    - Provision an EC2 instance

TODO: consul cloud autojoin functionality
"""
from pulumi import ResourceOptions, StackReference, export
from pulumi.config import get_config
from pulumi_aws import ec2, iam, route53, s3

from ol_infrastructure.lib.aws.ec2_helper import build_userdata, debian_10_ami
from ol_infrastructure.lib.aws.iam_helper import lint_iam_policy
from ol_infrastructure.lib.ol_types import AWSBase
from ol_infrastructure.lib.pulumi_helper import parse_stack
from ol_infrastructure.providers.salt.minion import (
    OLSaltStackMinion,
    OLSaltStackMinionInputs,
)

# Setup

# Lookup dict for environment name only as needed for minion grain, for
# matching up with legacy infrastructure.
env_nomenclature = {
    "dev": "applications-dev",
    "qa": "rc-apps",
    "production": "production-apps",
}

stack_info = parse_stack()
network_stack = StackReference(f"infrastructure.aws.network.{stack_info.name}")
dns_stack = StackReference("infrastructure.aws.dns")
mitodl_zone_id = dns_stack.require_output("odl_zone_id")
apps_vpc = network_stack.require_output("applications_vpc")
ocw_next_build_environment = f"applications-{stack_info.env_suffix}"
aws_config = AWSBase(
    tags={"OU": "open-courseware", "Environment": ocw_next_build_environment},
)

# Website bucket and related IAM stuff

website_bucket_name = f"ocw-website-{ocw_next_build_environment}"
website_bucket = s3.Bucket(
    website_bucket_name,
    bucket=website_bucket_name,
    acl="private",
    tags=aws_config.tags,
    versioning={"enabled": True},
)

# Instance profile that includes access to that bucket, plus others
ocw_next_instance_policy = {
    "Version": "2012-10-17",
    "Statement": [
        {
            "Sid": "Statement1",
            "Effect": "Allow",
            "Action": ["s3:List*", "s3:Get*"],
            "Resource": [
                # This bucket is not managed here, but we need access:
                "arn:aws:s3:::open-learning-course-data-*",
                "arn:aws:s3:::open-learning-course-data-*/*",
            ],
        },
        {
            "Sid": "Statement3",
            "Effect": "Allow",
            "Action": ["s3:List*", "s3:Get*", "s3:Put*", "s3:Delete*"],
            "Resource": [
                f"arn:aws:s3:::{website_bucket_name}",
                f"arn:aws:s3:::{website_bucket_name}/*",
            ],
        },
    ],
}

iam_instance_policy = iam.Policy(
    f"ocw-build-instance-policy-{stack_info.env_suffix}",
    name=f"ocw-build-instance-policy-{stack_info.env_suffix}",
    path=f"/ol-applications/ocw-build-policy-{stack_info.env_suffix}/",
    policy=lint_iam_policy(ocw_next_instance_policy, stringify=True),
    description="Grants access to S3 buckets from the OCW Build server",
)

iam_role = iam.Role(
    f"ocw-build-instance-role-{stack_info.env_suffix}",
    assume_role_policy=lint_iam_policy(
        {
            "Version": "2012-10-17",
            "Statement": {
                "Effect": "Allow",
                "Action": "sts:AssumeRole",
                "Principal": {"Service": "ec2.amazonaws.com"},
            },
        },
        stringify=True,
    ),
    name=f"ocw-build-instance-role-{stack_info.env_suffix}",
    path="/ol-applications/ocw-build-role/",
)

iam.RolePolicyAttachment(
    f"ocw-build-role-policy-{stack_info.env_suffix}",
    policy_arn=iam_instance_policy.arn,
    role=iam_role.name,
)

ocw_build_instance_profile = iam.InstanceProfile(
    f"ocw-build-instance-profile-{stack_info.env_suffix}",
    role=iam_role.name,
    name=f"ocw-build-instance-profile-{stack_info.env_suffix}",
    path="/ol-applications/ocw-build-instance-profile/",
)

# Salt minion

ocw_build_minion_id = f"ocw-build-{ocw_next_build_environment}-0"
salt_minion = OLSaltStackMinion(
    f"saltstack-minion-{ocw_build_minion_id}",
    OLSaltStackMinionInputs(
        minion_id=ocw_build_minion_id,
        salt_api_url=get_config("saltstack:api_url"),
        salt_user=get_config("saltstack:api_user"),
        salt_password=get_config("saltstack:api_password"),
    ),
)

cloud_init_userdata = build_userdata(
    instance_name=ocw_build_minion_id,
    minion_keys=salt_minion,
    minion_roles=["ocw-build"],
    minion_environment=env_nomenclature[stack_info.env_suffix],
    salt_host=f"salt-{stack_info.env_suffix}.private.odl.mit.edu",
)

instance_tags = aws_config.merged_tags({"Name": ocw_build_minion_id})
ec2_instance = ec2.Instance(
    f"ocw-build-instance-{ocw_next_build_environment}",
    ami=debian_10_ami.id,
    user_data=cloud_init_userdata,
    instance_type=get_config("ocw_build:instance_type"),
    iam_instance_profile=ocw_build_instance_profile.id,
    tags=instance_tags,
    volume_tags=instance_tags,
    subnet_id=apps_vpc["subnet_ids"][0],
    key_name=get_config("saltstack:key_name"),
    root_block_device=ec2.InstanceRootBlockDeviceArgs(
        volume_type="gp2", volume_size=100
    ),
    vpc_security_group_ids=[
        apps_vpc["security_groups"]["default"],
        apps_vpc["security_groups"]["web"],
        apps_vpc["security_groups"]["salt_minion"],
    ],
    opts=ResourceOptions(depends_on=[salt_minion]),
)

# DNS

fifteen_minutes = 60 * 15
route53_domain = route53.Record(
    f"ocw-build-{stack_info.env_suffix}-service-domain",
    name=get_config("ocw_build:domain"),
    type="A",
    ttl=fifteen_minutes,
    records=[ec2_instance.public_ip],
    zone_id=mitodl_zone_id,
    opts=ResourceOptions(depends_on=[ec2_instance]),
)
route53_domain_v6 = route53.Record(
    f"ocw-build-{stack_info.env_suffix}-service-domain-v6",
    name=get_config("ocw_build:domain"),
    type="AAAA",
    ttl=fifteen_minutes,
    records=ec2_instance.ipv6_addresses,
    zone_id=mitodl_zone_id,
    opts=ResourceOptions(depends_on=[ec2_instance]),
)

export(
    "ocw_build_app",
    {
        "ec2_private_address": ec2_instance.private_ip,
        "ec2_public_address": ec2_instance.public_ip,
        "ec2_address_v6": ec2_instance.ipv6_addresses,
    },
)
