import json

from pulumi import Config, ResourceOptions, export
from pulumi.stack_reference import StackReference
from pulumi_aws import ec2, iam

from ol_infrastructure.lib.aws.ec2_helper import (
    InstanceTypes,
    build_userdata,
    debian_10_ami,
    default_egress_args,
)
from ol_infrastructure.lib.ol_types import AWSBase
from ol_infrastructure.lib.pulumi_helper import parse_stack
from ol_infrastructure.providers.salt.minion import (
    OLSaltStackMinion,
    OLSaltStackMinionInputs,
)

stack_info = parse_stack()
env_config = Config("environment")
mongodb_config = Config("mongodb")
salt_config = Config("saltstack")
environment_name = f"{stack_info.env_prefix}-{stack_info.env_suffix}"
business_unit = env_config.get("business_unit") or "operations"
aws_config = AWSBase(tags={"OU": business_unit, "Environment": environment_name})
network_stack = StackReference(f"infrastructure.aws.network.{stack_info.name}")
policy_stack = StackReference("infrastructure.aws.policies")
operations_vpc = network_stack.require_output("operations_vpc")
consul_stack = StackReference(
    "infrastructure.consul."  # noqa: WPS237, WPS221
    f"{stack_info.namespace.rsplit('.', 1)[1]}."  # noqa: WPS237, WPS221
    f"{stack_info.name}"
)
destination_vpc = network_stack.require_output(env_config.require("vpc_reference"))

mongodb_instance_role = iam.Role(
    "mongodb-instance-role",
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
    name=f"mongodb-instance-role-{environment_name}",
    path=f"/ol-infrastructure/mongodb-{environment_name}/",
    tags=aws_config.tags,
)

iam.RolePolicyAttachment(
    f"mongodb-role-policy-{environment_name}",
    policy_arn=policy_stack.require_output("iam_policies")["describe_instances"],
    role=mongodb_instance_role.name,
)

mongodb_instance_profile = iam.InstanceProfile(
    f"mongodb-instance-profile-{environment_name}",
    role=mongodb_instance_role.name,
    name=f"mongodb-instance-profile-{environment_name}",
    path="/ol-infrastructure/mongodb-profile/",
)

mongodb_security_group = ec2.SecurityGroup(
    f"mongodb-{environment_name}",
    name=f"mongodb-{environment_name}",
    description="Access control between Mongodb instances in cluster",
    tags=aws_config.merged_tags({"Name": f"{environment_name}-mongodb"}),
    vpc_id=destination_vpc["id"],
    ingress=[
        ec2.SecurityGroupIngressArgs(
            protocol="tcp",
            from_port=27017,  # noqa: WPS432
            to_port=27017,  # noqa: WPS432
            cidr_blocks=[destination_vpc["cidr"]],
            description="Access to Mongodb cluster from VPC",
        ),
        ec2.SecurityGroupIngressArgs(
            cidr_blocks=[operations_vpc["cidr"]],
            ipv6_cidr_blocks=[operations_vpc["cidr_v6"]],
            protocol="tcp",
            from_port=27017,  # noqa: WPS432
            to_port=27017,  # noqa: WPS432
            description="Access to Mongodb cluster from operations VPC",
        ),
    ],
    egress=default_egress_args,
)

security_groups = {"mongodb_server": mongodb_security_group.id}

instance_type_name = mongodb_config.get("instance_type") or InstanceTypes.medium.name
instance_type = InstanceTypes[instance_type_name].value
mongodb_instances = []
export_data = {}
subnets = destination_vpc["subnet_ids"]
salt_environment = Config("saltstack").get("environment_name") or environment_name
instance_nums = range(mongodb_config.get_int("instance_count") or 3)

for instance_num, subnet in zip(instance_nums, subnets):
    instance_name = f"mongodb-{environment_name}-{instance_num}"
    salt_minion = OLSaltStackMinion(
        f"saltstack-minion-{instance_name}",
        OLSaltStackMinionInputs(minion_id=instance_name),
    )

    cloud_init_userdata = build_userdata(
        instance_name=instance_name,
        minion_keys=salt_minion,
        minion_roles=["mongodb"],
        minion_environment=salt_environment,
        salt_host=f"salt-{stack_info.env_suffix}.private.odl.mit.edu",
        additional_salt_grains={"business_unit": business_unit},
        additional_cloud_config={
            "fs_setup": [
                {
                    "device": "/dev/nvme0n1",
                    "filesystem": "ext4",
                    "label": "var_lib_mongodb",
                    "partition": "None",
                }
            ],
            "mounts": [
                [
                    "LABEL=var_lib_mongodb",
                    "/var/lib/mongodb",
                    "ext4",
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
            "mongodb_env": environment_name,
        }
    )
    mongodb_security_groups = [
        destination_vpc["security_groups"]["salt_minion"],
        consul_stack.require_output("security_groups")["consul_agent"],
        mongodb_security_group.id,
    ]
    mongodb_instance = ec2.Instance(
        f"mongodb-instance-{environment_name}-{instance_num}",
        ami=mongodb_config.get("ami_id") or debian_10_ami.id,
        user_data=cloud_init_userdata,
        instance_type=instance_type,
        iam_instance_profile=mongodb_instance_profile.id,
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
                volume_size=mongodb_config.get_int("disk_size") or 100,
                encrypted=True,
            )
        ],
        vpc_security_group_ids=mongodb_security_groups,
        opts=ResourceOptions(depends_on=[salt_minion]),
    )
    mongodb_instances.append(mongodb_instance)

    export_data[instance_name] = {
        "public_ip": mongodb_instance.public_ip,
        "private_ip": mongodb_instance.private_ip,
        "ipv6_address": mongodb_instance.ipv6_addresses,
    }
export(
    "security_groups",
    {
        "mongodb_server": mongodb_security_group.id,
    },
)

export("instances", export_data)
