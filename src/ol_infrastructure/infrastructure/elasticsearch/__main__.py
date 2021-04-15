import json
from itertools import chain

from pulumi import Config, ResourceOptions
from pulumi.stack_reference import StackReference
from pulumi_aws import ec2, iam, s3

from ol_infrastructure.lib.aws.ec2_helper import (
    InstanceTypes,
    build_userdata,
    debian_10_ami,
    default_egress_args,
)
from ol_infrastructure.lib.aws.iam_helper import lint_iam_policy
from ol_infrastructure.lib.ol_types import AWSBase
from ol_infrastructure.lib.pulumi_helper import parse_stack
from ol_infrastructure.providers.salt.minion import (
    OLSaltStackMinion,
    OLSaltStackMinionInputs,
)

stack_info = parse_stack()
env_config = Config("environment")
elasticsearch_config = Config("elasticsearch")
salt_config = Config("saltstack")
environment_name = f"{stack_info.env_prefix}-{stack_info.env_suffix}"
business_unit = env_config.get("business_unit") or "operations"
aws_config = AWSBase(tags={"OU": business_unit, "Environment": environment_name})
network_stack = StackReference(f"infrastructure.aws.network.{stack_info.name}")
consul_stack = StackReference(
    f"infrastructure.consul.{stack_info.namespace.rsplit('.', 1)[1]}."  # noqa: WPS237
    "{stack_info.name}"
)
destination_vpc = network_stack.require_output(env_config.require("vpc_reference"))
elasticsearch_backup_bucket_name = f"ol-{environment_name}-elasticsearch-backup"
elasticsearch_backup_bucket = s3.Bucket(
    f"ol-{environment_name}-elasticsearch-backup",
    bucket=elasticsearch_backup_bucket_name,
    acl="private",
    tags=aws_config.tags,
    versioning={"enabled": True},
    server_side_encryption_configuration={
        "rule": {
            "applyServerSideEncryptionByDefault": {
                "sseAlgorithm": "aws:kms",
            },
        },
    },
)

elasticsearch_instance_policy = {
    "Version": "2012-10-17",
    "Statement": [
        {
            "Action": [
                "ec2:DescribeInstances",
                "ec2:DescribeAvailabilityZones",
                "ec2:DescribeRegions",
                "ec2:DescribeSecurityGroups",
                "ec2:DescribeTags",
            ],
            "Effect": "Allow",
            "Resource": ["*"],
        },
        {
            "Action": [
                "s3:AbortMultipartUpload",
                "s3:ListBucket",
                "s3:GetObject*",
                "s3:PutObject",
                "s3:DeleteObject",
            ],
            "Effect": "Allow",
            "Resource": [
                f"arn:aws:s3:::{elasticsearch_backup_bucket_name}",
                f"arn:aws:s3:::{elasticsearch_backup_bucket_name}/*",
            ],
        },
    ],
}

elasticsearch_iam_policy = iam.Policy(
    f"elasticsearch-policy-{environment_name}",
    name=f"elasticsearch-policy-{environment_name}",
    path=f"/ol-applications/elasticsearch-{environment_name}/",
    policy=lint_iam_policy(elasticsearch_instance_policy, stringify=True),
    description="Policy to access resources needed by elasticsearch instances",
)

elasticsearch_instance_role = iam.Role(
    "elasticsearch-instance-role",
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
    name=f"elasticsearch-instance-role-{environment_name}",
    path=f"/ol-operations/elasticsearch-{environment_name}/",
    tags=aws_config.tags,
)

iam.RolePolicyAttachment(
    f"elasticsearch-role-policy-{environment_name}",
    policy_arn=elasticsearch_iam_policy.arn,
    role=elasticsearch_instance_role.name,
)

elasticsearch_instance_profile = iam.InstanceProfile(
    f"elasticsearch-instance-profile-{environment_name}",
    role=elasticsearch_instance_role.name,
    name=f"elasticsearch-instance-profile-{environment_name}",
    path="/ol-operations/elasticsearch-profile/",
)

elasticsearch_security_group = ec2.SecurityGroup(
    f"elasticsearch-{environment_name}",
    name=f"elasticsearch-{environment_name}",
    description="Access control between Elasticsearch instances in cluster",
    tags=aws_config.merged_tags({"Name": f"{environment_name}-elasticsearch"}),
    vpc_id=destination_vpc["id"],
    ingress=[
        ec2.SecurityGroupIngressArgs(
            protocol="tcp",
            from_port=9200,  # noqa: WPS432
            to_port=9200,  # noqa: WPS432
            cidr_blocks=[destination_vpc["cidr"]],
            description="Access to Elasticsearch cluster from VPC",
        ),
        ec2.SecurityGroupIngressArgs(
            self=True,
            protocol="tcp",
            from_port=9300,  # noqa: WPS432
            to_port=9400,  # noqa: WPS432
            description="Elasticsearch cluster instances access",
        ),
    ],
    egress=default_egress_args,
)

security_groups = {"elasticsearch_server": elasticsearch_security_group.id}

instance_type_name = (
    elasticsearch_config.get("instance_type") or InstanceTypes.medium.name
)
instance_type = InstanceTypes[instance_type_name].value
elasticsearch_instances = []
export_data = {}
subnets = destination_vpc["subnet_ids"]
subnet_id = subnets.apply(chain)
salt_environment = Config("saltstack").get("environment_name") or environment_name
instance_nums = range(elasticsearch_config.get_int("instance_count") or 3)

for instance_num, subnet in zip(instance_nums, subnets):
    instance_name = f"elasticsearch-{environment_name}-{instance_num}"
    salt_minion = OLSaltStackMinion(
        f"saltstack-minion-{instance_name}",
        OLSaltStackMinionInputs(minion_id=instance_name),
    )

    cloud_init_userdata = build_userdata(
        instance_name=instance_name,
        minion_keys=salt_minion,
        minion_roles=["elasticsearch"],
        minion_environment=salt_environment,
        salt_host=f"salt-{stack_info.env_suffix}.private.odl.mit.edu",
        additional_cloud_config={
            "fs_setup": [
                {
                    "device": "/dev/nvme0n1",
                    "filesystem": "ext4",
                    "label": "var_lib_elastics",
                    "partition": "None",
                }
            ],
            "mounts": [
                [
                    "LABEL=var_lib_elastics",
                    "/var/lib/elasticsearch",
                    "auto",
                    "defaults",
                    "0",
                    "0",
                ]
            ],
        },
    )

    instance_tags = aws_config.merged_tags(
        {
            "Name": instance_name,
            "elasticsearch_env": environment_name,
            "escluster": salt_environment,
        }
    )
    es_security_groups = [
        destination_vpc["security_groups"]["salt_minion"],
        consul_stack.require_output("security_groups")["consul_agent"],
        elasticsearch_security_group.id,
    ]
    if elasticsearch_config.get_bool("public_web"):
        es_security_groups.append(destination_vpc["security_groups"]["web"])
    elasticsearch_instance = ec2.Instance(
        f"elasticsearch-instance-{environment_name}-{instance_num}",
        ami=debian_10_ami.id,
        user_data=cloud_init_userdata,
        instance_type=instance_type,
        iam_instance_profile=elasticsearch_instance_profile.id,
        tags=instance_tags,
        volume_tags=instance_tags,
        subnet_id=subnet,
        key_name=salt_config.require("key_name"),
        root_block_device=ec2.InstanceRootBlockDeviceArgs(
            volume_type="gp2",
            volume_size=20,  # noqa: WPS432
            encrypted=True,
        ),
        ebs_block_devices=[
            ec2.InstanceEbsBlockDeviceArgs(
                device_name="/dev/sdb",
                volume_type="gp2",
                volume_size=elasticsearch_config.get_int("disk_size") or 100,
                encrypted=True,
            )
        ],
        vpc_security_group_ids=es_security_groups,
        opts=ResourceOptions(depends_on=[salt_minion]),
    )
    elasticsearch_instances.append(elasticsearch_instance)

    export_data[instance_name] = {
        "public_ip": elasticsearch_instance.public_ip,
        "private_ip": elasticsearch_instance.private_ip,
        "ipv6_address": elasticsearch_instance.ipv6_addresses,
    }
