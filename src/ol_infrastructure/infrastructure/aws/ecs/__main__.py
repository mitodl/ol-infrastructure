import base64
import json

import pulumi
import yaml
from pulumi import ResourceOptions
from pulumi_aws import autoscaling, ec2, ecs, ssm

from ol_infrastructure.lib.ol_types import AWSBase
from ol_infrastructure.lib.pulumi_helper import parse_stack

SEARCH_DOMAIN_NAME_MAX_LENGTH = 28

###############
# STACK SETUP #
###############
cluster_config = pulumi.Config("cluster")
env_config = pulumi.Config("environment")
stack_info = parse_stack()

network_stack = pulumi.StackReference(f"infrastructure.aws.network.{stack_info.name}")
consul_stack = pulumi.StackReference(
    f"infrastructure.consul.{stack_info.env_prefix}.{stack_info.name}"
)
vault_stack = pulumi.StackReference(
    f"infrastructure.vault.operations.{stack_info.name}"
)

#############
# VARIABLES #
#############
environment_name = f"{stack_info.env_prefix}-{stack_info.env_suffix}"
target_vpc = network_stack.require_output(env_config.require("target_vpc"))
business_unit = env_config.get("business_unit") or "operations"
aws_config = AWSBase(tags={"OU": business_unit, "Environment": environment_name})

cluster_name = f"{environment_name}-ecs-cluster"
user_data = f"""#!/bin/bash
echo ECS_CLUSTER={cluster_name} >> /etc/ecs/ecs.config"""
user_data = base64.b64encode(
    "#cloud-config\n{}".format(
        yaml.dump(
            {
                "write_files": [
                    {
                        "path": "/etc/ecs/ecs.config",
                        "content": f"ECS_CLUSTER={cluster_name}",
                        "owner": "root:root",
                    }
                ],
            },
        )
    ).encode("utf8")
).decode("utf8")

# )  # Default is for legacy compatability

##########
# CREATE #
##########

ecs_image_ssm_path = "/aws/service/ecs/optimized-ami/amazon-linux-2023/recommended"
ecs_ami_id = json.loads(ssm.get_parameter(name=ecs_image_ssm_path).value)["image_id"]

launch_template = ec2.LaunchTemplate(
    f"{environment_name}-ecs-launch-template",
    image_id=ecs_ami_id,
    instance_type=cluster_config.get("instance_size") or "t3a.medium",
    user_data=user_data,
)
autoscaling_group_tags = [
    autoscaling.GroupTagArgs(
        key=key_name,
        value=key_value,
        propagate_at_launch=True,
    )
    for key_name, key_value in aws_config.merged_tags(
        {"ami_id": ecs_ami_id, "AmazonECSManaged": "true"}
    ).items()
]

autoscaling_group = autoscaling.Group(
    f"{environment_name}-ecs-autoscalinggroup",
    max_size=cluster_config.get("max_size") or 3,
    min_size=cluster_config.get("min_size") or 2,
    desired_capacity=cluster_config.get("desired_size") or 2,
    vpc_zone_identifiers=target_vpc["subnet_ids"],
    launch_template=autoscaling.GroupLaunchTemplateArgs(
        id=launch_template.id,
        version="$Latest",
    ),
    instance_refresh=autoscaling.GroupInstanceRefreshArgs(
        strategy="Rolling",
        preferences=autoscaling.GroupInstanceRefreshPreferencesArgs(
            min_healthy_percentage=50,
        ),
        triggers=["tags"],
    ),
    tags=autoscaling_group_tags,
)

capacity_provider = ecs.CapacityProvider(
    f"{environment_name}-ecs-capacity-provider",
    auto_scaling_group_provider=ecs.CapacityProviderAutoScalingGroupProviderArgs(
        auto_scaling_group_arn=autoscaling_group.arn,
        managed_termination_protection="DISABLED",
        managed_scaling=ecs.CapacityProviderAutoScalingGroupProviderManagedScalingArgs(
            instance_warmup_period=300,
            maximum_scaling_step_size=1,
            minimum_scaling_step_size=1,
            status="DISABLED",
            target_capacity=10,
        ),
    ),
)

ecs_cluster = ecs.Cluster(
    cluster_name,
    name=cluster_name,
    tags=aws_config.merged_tags(),
    opts=ResourceOptions(depends_on=[capacity_provider]),
)

cluster_capacity_provider = ecs.ClusterCapacityProviders(
    f"{environment_name}-ecs-cluster-capacity-provider",
    capacity_providers=[capacity_provider.name],
    cluster_name=ecs_cluster.name,
    opts=ResourceOptions(depends_on=[capacity_provider, ecs_cluster]),
)


#
#
#
#
## Consul Service
#    "consul-provider",
#        read_yaml_secrets(Path(f"pulumi/consul.{stack_info.env_suffix}.yaml"))[
#            "basic_auth_password"
#    ),
#    "aws-opensearch-consul-node",
#    "aws-opensearch-consul-service",
#        "external-node": True,
#        "external-probe": True,
#    },
#        consul.ServiceCheckArgs(
#            ).apply(lambda es: "{address}:{port}".format(**es)),
#    ],
#
# consul.Keys(
#    "elasticsearch",
#        consul.KeysKeyArgs(
#        ),
#    ],
#
#
## Export Resources for shared use
# pulumi.export(
#    "cluster",
#    },
