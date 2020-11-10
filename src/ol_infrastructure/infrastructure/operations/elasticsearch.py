import json

from itertools import chain

from pulumi import Config, ResourceOptions, export
from pulumi_aws import ec2, iam

from ol_infrastructure.infrastructure import operations as ops
from ol_infrastructure.lib.aws.ec2_helper import (
    InstanceTypes,
    build_userdata,
    debian_10_ami
)
from ol_infrastructure.lib.aws.iam_helper import lint_iam_policy
from ol_infrastructure.lib.ol_types import AWSBase
from ol_infrastructure.providers.salt.minion import (
    OLSaltStackMinion,
    OLSaltStackMinionInputs
)

env_config = Config('environment')
elasticsearch_config = Config('elasticsearch')
salt_config = Config('saltstack')
environment_name = f'{ops.env_prefix}-{ops.env_suffix}'
business_unit = env_config.get('business_unit') or 'operations'
aws_config = AWSBase(tags={
    'OU': business_unit,
    'Environment': environment_name
})
destination_vpc = ops.network_stack.require_output(env_config.require('vpc_reference'))

elasticsearch_instance_policy = {
    "Version": "2012-10-17",
    "Statement": [
        {
            "Action": [
                "ec2:DescribeInstances",
                "ec2:DescribeAvailabilityZones",
                "ec2:DescribeRegions",
                "ec2:DescribeSecurityGroups",
                "ec2:DescribeTags"
            ],
            "Effect": "Allow",
            "Resource": [
                "*"
            ]
        },
        {
            "Action": [
                "s3:ListBucket",
                "s3:GetBucketLocation",
                "s3:ListBucketMultipartUploads",
                "s3:ListBucketVersions"
            ],
            "Effect": "Allow",
            "Resource": [
                "arn:aws:s3:::mitx-elasticsearch-backups",
                "arn:aws:s3:::mitx-elasticsearch-backups-operations-es7"
            ]
        },
        {
            "Action": [
                "s3:GetObject",
                "s3:PutObject",
                "s3:DeleteObject",
                "s3:AbortMultipartUpload",
                "s3:ListMultipartUploadParts"
            ],
            "Effect": "Allow",
            "Resource": [
                "arn:aws:s3:::mitx-elasticsearch-backups/*",
                "arn:aws:s3:::mitx-elasticsearch-backups-operations-es7/*"
            ]
        }
    ],
}

elasticsearch_iam_policy = iam.Policy(
    f'elasticsearch-policy-{environment_name}',
    name=f'elasticsearch-policy-{environment_name}',
    path=f'/ol-applications/elasticsearch-{environment_name}/',
    policy=lint_iam_policy(elasticsearch_instance_policy, stringify=True),
    description='Policy for granting access to backup S3 buckets'
)

elasticsearch_instance_role = iam.Role(
    'elasticsearch-instance-role',
    assume_role_policy=json.dumps(
        {
            'Version': '2012-10-17',
            'Statement': {
                'Effect': 'Allow',
                'Action': 'sts:AssumeRole',
                'Principal': {'Service': 'ec2.amazonaws.com'}
            }
        }
    ),
    name=f'elasticsearch-instance-role-{environment_name}',
    path=f'/ol-operations/elasticsearch-{environment_name}',
    tags=aws_config.tags
)

iam.RolePolicyAttachment(
    f'elasticsearch-role-policy-{environment_name}',
    policy_arn=ops.policy_stack.require_output('iam_policies')['describe_instances'],
    role=elasticsearch_instance_role.name
)

elasticsearch_instance_profile = iam.InstanceProfile(
    f'elasticsearch-instance-profile-{environment_name}',
    role=elasticsearch_instance_role.name,
    name=f'elasticsearch-instance-profile-{environment_name}',
    path='/ol-operations/elasticsearch-profile/'
)

elasticsearch_security_group = ec2.SecurityGroup(
    f'elasticsearch-{environment_name}',
    name=f'elasticsearch-{environment_name}',
    description='Access control between Elasticsearch instances in cluster',
    tags=aws_config.merged_tags({'Name': f'{environment_name}-elasticsearch'}),
    vpc_id=destination_vpc['id'],
    ingress=[
        ec2.SecurityGroupIngressArgs(
            self=True,
            protocol='tcp',
            from_port=9300,
            to_port=9400,
            description='Elasticsearch cluster instances access'
        )
    ]
)

instance_type_name = elasticsearch_config.get('instance_type') or InstanceTypes.medium.name
instance_type = InstanceTypes[instance_type_name].value
elasticsearch_instances = []
elasticsearch_export = {}
subnets = destination_vpc['subnet_ids']
subnet_id = subnets.apply(chain)
for count, subnet in zip(range(elasticsearch_config.get_int('instance_count') or 3), subnets):  # type: ignore # noqa: WPS221
    instance_name = f'elasticsearch-{environment_name}-{count}'
    salt_minion = OLSaltStackMinion(
        f'saltstack-minion-{instance_name}',
        OLSaltStackMinionInputs(
            minion_id=instance_name
        )
    )

    cloud_init_userdata = build_userdata(
        instance_name=instance_name,
        minion_keys=salt_minion,
        minion_roles=['elasticsearch', 'service_discovery'],
        minion_environment=environment_name,
        salt_host=f'salt-{ops.env_suffix}.private.odl.mit.edu')

    instance_tags = aws_config.merged_tags({
        'Name': instance_name,
        'elasticsearch_env': environment_name
    })
    elasticsearch_instance = ec2.Instance(
        f'elasticsearch-instance-{environment_name}-{count}',
        ami=debian_10_ami.id,
        user_data=cloud_init_userdata,
        instance_type=instance_type,
        iam_instance_profile=elasticsearch_instance_profile.id,
        tags=instance_tags,
        volume_tags=instance_tags,
        subnet_id=subnet,
        key_name=salt_config.require('key_name'),
        root_block_device=ec2.InstanceRootBlockDeviceArgs(
            volume_type='gp2',
            volume_size=20,
            encrypted=True,
        ),
        ebs_block_devices=ec2.InstanceEbsBlockDeviceArgs(
            device_name='/dev/xvdb',
            volume_type='gp2',
            volume_size=100,
            encrypted=True,
        ),
        vpc_security_group_ids=[
            destination_vpc['security_groups']['salt_minion'],
            elasticsearch_security_group.id
        ],
        opts=ResourceOptions(depends_on=[salt_minion])
    )
    elasticsearch_instances.append(elasticsearch_instance)

    elasticsearch_export[instance_name] = {
        'public_ip': elasticsearch_instance.public_ip,
        'private_ip': elasticsearch_instance.private_ip,
        'ipv6_address': elasticsearch_instance.ipv6_addresses
    }

export('elasticsearch', {
    'elasticsearch_security_group': elasticsearch_security_group,
    'instances': elasticsearch_export
})
