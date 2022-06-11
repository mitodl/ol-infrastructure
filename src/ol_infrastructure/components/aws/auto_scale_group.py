from typing import Dict, List, Optional, Union

import pulumi
from pulumi_aws.acm import get_certificate
from pulumi_aws.autoscaling import (
    Group,
    GroupInstanceRefreshArgs,
    GroupInstanceRefreshPreferencesArgs,
    GroupLaunchTemplateArgs,
    GroupTagArgs,
)
from pulumi_aws.ec2 import (
    LaunchTemplate,
    LaunchTemplateBlockDeviceMappingArgs,
    LaunchTemplateBlockDeviceMappingEbsArgs,
    LaunchTemplateIamInstanceProfileArgs,
    LaunchTemplateMetadataOptionsArgs,
    LaunchTemplateTagSpecificationArgs,
    SecurityGroup,
)
from pulumi_aws.lb import (
    Listener,
    ListenerArgs,
    ListenerDefaultActionArgs,
    LoadBalancer,
    TargetGroup,
    TargetGroupHealthCheckArgs,
)
from pydantic import BaseModel, NonNegativeInt, PositiveInt, validator

from bridge.lib.magic_numbers import (
    AWS_LOAD_BALANCER_NAME_MAX_LENGTH,
    AWS_TARGET_GROUP_NAME_MAX_LENGTH,
    DEFAULT_HTTPS_PORT,
)
from ol_infrastructure.lib.aws.ec2_helper import DiskTypes, InstanceTypes
from ol_infrastructure.lib.ol_types import AWSBase


class BlockDeviceMapping(BaseModel):
    """Container for describing a block device mapping for an EC2 instance/launch template"""

    delete_on_termination: bool = True
    device_name: str = "/dev/xvda"
    volume_size: PositiveInt = PositiveInt(25)
    volume_type: DiskTypes = DiskTypes.ssd

    class Config:
        arbitrary_types_allowed = True


class TagSpecification(BaseModel):
    """Container for describing a tag specification for an EC2 launch template"""

    resource_type: str
    tags: Dict[str, str]

    class Config:
        arbitrary_types_allowed = True


class OLTargetGroupConfig(AWSBase):
    """Configuration object for defining a target group for use with a loadbalancer."""

    vpc_id: Union[str, pulumi.Output[str]]
    port: NonNegativeInt = NonNegativeInt(443)
    protocol: str = "HTTPS"

    health_check_enabled: bool = True
    health_check_healthy_threshold: PositiveInt = PositiveInt(2)
    health_check_interval: PositiveInt = PositiveInt(10)
    health_check_matcher: str = "200"
    health_check_path: str = "/"
    health_check_port: str = str(DEFAULT_HTTPS_PORT)
    health_check_protocol: str = "HTTPS"
    health_check_timeout: PositiveInt = PositiveInt(5)
    health_check_unhealthy_threshold: PositiveInt = PositiveInt(3)

    class Config:
        arbitrary_types_allowed = True


class OLLoadBalancerConfig(AWSBase):
    """Configuration object for defining a load balancer object for use with an autoscale group"""

    enable_http2: bool = True
    internal: bool = False
    ip_address_type: str = "dualstack"
    load_balancer_type: str = "application"
    port: PositiveInt = PositiveInt(DEFAULT_HTTPS_PORT)
    security_groups: List[SecurityGroup]
    subnets: pulumi.Output[str]

    listener_use_acm: bool = True
    listener_cert_domain: str = "*.odl.mit.edu"
    listener_protocol: str = "HTTPS"
    listener_action_type: str = "forward"

    class Config:
        arbitrary_types_allowed = True

    @validator("ip_address_type")
    def is_valid_ip_address_type(
        cls: "OLLoadBalancerConfig", ip_address_type: str
    ) -> str:
        if ip_address_type not in ["dualstack", "ipv4"]:
            raise ValueError(
                f"ip_address_type: {ip_address_type} is not valid. Only 'dualstack' and 'ipv4 are accepted'"
            )
        return ip_address_type

    @validator("load_balancer_type")
    def is_valid_load_balancer_type(
        cls: "OLLoadBalancerConfig", load_balancer_type: str
    ) -> str:
        if load_balancer_type not in ["application", "gateway", "network"]:
            raise ValueError(
                f"load_balancer_type: {load_balancer_type} is not valid. Only 'application', 'gateway', or 'network' are accepted"
            )
        return load_balancer_type


class OLLaunchTemplateConfig(AWSBase):
    """Configuration Object for defining configuration needed to create a launch template."""

    block_device_mappings: List[BlockDeviceMapping]
    image_id: str
    instance_profile_arn: Union[str, pulumi.Output[str]]
    instance_type: InstanceTypes = InstanceTypes.burstable_medium
    key_name: str = "oldevops"
    security_groups: List[Union[SecurityGroup, pulumi.Output]]
    tag_specifications: List[TagSpecification]
    user_data: Optional[Union[str, pulumi.Output[str]]]

    class Config:
        arbitrary_types_allowed = True


class OLAutoScaleGroupConfig(AWSBase):
    """Configuration Object for defining configuration needed to create an autoscale group."""

    asg_name: str
    aws_config: AWSBase
    desired_size: PositiveInt = PositiveInt(2)
    health_check_grace_period: NonNegativeInt = NonNegativeInt(0)
    health_check_type: str = "ELB"
    max_size: PositiveInt = PositiveInt(2)
    min_size: PositiveInt = PositiveInt(1)
    vpc_zone_identifiers: pulumi.Output[str]

    instance_refresh_checkpoint_delay: PositiveInt = PositiveInt(3600)
    instance_refresh_checkpoint_percentages: List[NonNegativeInt] = []
    instance_refresh_warmup: PositiveInt = PositiveInt(health_check_grace_period)
    instance_refresh_min_healthy_percentage: NonNegativeInt = NonNegativeInt(50)
    instance_refresh_strategy: str = "Rolling"
    instance_refresh_triggers: List[str] = ["tags"]

    class Config:
        arbitrary_types_allowed = True

    @validator("instance_refresh_strategy")
    def is_valid_strategy(cls: "OLAutoScaleGroupConfig", strategy: str) -> str:
        if strategy != "Rolling":
            raise ValueError("The only vaild instance refresh strategy is 'Rolling'")
        return strategy

    @validator("health_check_type")
    def is_valid_healthcheck(
        cls: "OLAutoScaleGroupConfig", health_check_type: str
    ) -> str:
        if health_check_type not in ["ELB", "EC2"]:
            raise ValueError(
                f"health_check_type: {health_check_type} is not valid. Only 'ELB' or 'EC2' are accepted."
            )
        return health_check_type


class OLAutoScaling(pulumi.ComponentResource):
    """Component to create an autoscaling group with defaults as well as managed associated resources"""

    load_balancer: LoadBalancer = None
    listener: Listener = None
    target_group: TargetGroup = None
    auto_scale_group: Group = None
    launch_template: LaunchTemplate = None

    def __init__(
        self,
        asg_config: OLAutoScaleGroupConfig,
        lt_config: OLLaunchTemplateConfig,
        tg_config: Optional[OLTargetGroupConfig],
        lb_config: Optional[OLLoadBalancerConfig],
        opts: pulumi.ResourceOptions = None,
    ):

        super().__init__(
            "ol:infrastructure:aws:auto_scale_group:OLAutoScaleGroup",
            asg_config.asg_name,
            None,
            opts,
        )

        if bool(tg_config) != bool(lb_config):
            raise ValueError(
                "Both lb_config and tg_config must be provided if one is provided"
            )

        # Shared attributes
        resource_options = pulumi.ResourceOptions(parent=self).merge(opts)  # type: ignore
        resource_name_prefix = asg_config.asg_name + "-"

        # Create target group
        if tg_config:
            target_group_healthcheck = None
            if tg_config.health_check_enabled:
                target_group_healthcheck = TargetGroupHealthCheckArgs(
                    enabled=tg_config.health_check_enabled,
                    healthy_threshold=tg_config.health_check_healthy_threshold,
                    interval=tg_config.health_check_interval,
                    matcher=tg_config.health_check_matcher,
                    path=tg_config.health_check_path,
                    protocol=tg_config.health_check_protocol,
                    port=tg_config.health_check_port,
                    timeout=tg_config.health_check_timeout,
                    unhealthy_threshold=tg_config.health_check_unhealthy_threshold,
                )

            self.target_group = TargetGroup(
                resource_name_prefix + "target-group",
                name=(resource_name_prefix + "tg")[:AWS_TARGET_GROUP_NAME_MAX_LENGTH],
                vpc_id=tg_config.vpc_id,
                port=tg_config.port,
                protocol=tg_config.protocol,
                health_check=target_group_healthcheck,
                opts=resource_options,
            )

        # Create Load Balancer
        if lb_config:
            self.load_balancer = LoadBalancer(
                resource_name_prefix + "load-balancer",
                enable_http2=lb_config.enable_http2,
                internal=lb_config.internal,
                ip_address_type=lb_config.ip_address_type,
                load_balancer_type=lb_config.load_balancer_type,
                name=(resource_name_prefix + "lb")[:AWS_LOAD_BALANCER_NAME_MAX_LENGTH],
                security_groups=[group.id for group in lb_config.security_groups],
                subnets=lb_config.subnets,
                opts=resource_options,
            )

            # Create Load Balancer Listener (requires load balancer + target group first)
            listener_args = ListenerArgs(
                load_balancer_arn=self.load_balancer.arn,
                port=lb_config.port,
                protocol=lb_config.listener_protocol,
                default_actions=[
                    ListenerDefaultActionArgs(
                        type=lb_config.listener_action_type,
                        target_group_arn=self.target_group.arn,
                    )
                ],
            )
            if lb_config.listener_use_acm:
                listener_args.certificate_arn = get_certificate(
                    domain=lb_config.listener_cert_domain,
                    most_recent=True,
                    statuses=["ISSUED"],
                ).arn
            self.listener = Listener(
                resource_name_prefix + "load-balancer-listener",
                args=listener_args,
                opts=resource_options,
            )

        # Build list of block devices for launch template
        block_device_mappings = []
        for bdm in lt_config.block_device_mappings:
            block_device_mapping = LaunchTemplateBlockDeviceMappingArgs(
                device_name=bdm.device_name,
                ebs=LaunchTemplateBlockDeviceMappingEbsArgs(
                    volume_size=bdm.volume_size,
                    volume_type=bdm.volume_type,
                    delete_on_termination=bdm.delete_on_termination,
                ),
            )
            block_device_mappings.append(block_device_mapping)

        # Setup the tag specification objects
        tag_specifications = []
        for ts in lt_config.tag_specifications:
            tag_specification = LaunchTemplateTagSpecificationArgs(
                resource_type=ts.resource_type,
                tags=ts.tags,
            )
            tag_specifications.append(tag_specification)

        # Setup the instance refresh rules
        instance_refresh_policy = GroupInstanceRefreshArgs(
            strategy=asg_config.instance_refresh_strategy,
            triggers=asg_config.instance_refresh_triggers,
            preferences=GroupInstanceRefreshPreferencesArgs(
                checkpoint_delay=asg_config.instance_refresh_checkpoint_delay,
                checkpoint_percentages=asg_config.instance_refresh_checkpoint_percentages,
                instance_warmup=asg_config.instance_refresh_warmup,
                min_healthy_percentage=asg_config.instance_refresh_min_healthy_percentage,
            ),
        )

        # Construct the launch template
        self.launch_template = LaunchTemplate(
            f"{resource_name_prefix}-launch-template",
            name_prefix=resource_name_prefix,
            block_device_mappings=block_device_mappings,
            iam_instance_profile=LaunchTemplateIamInstanceProfileArgs(
                arn=lt_config.instance_profile_arn,
            ),
            image_id=lt_config.image_id,
            instance_type=lt_config.instance_type,
            key_name=lt_config.key_name,
            tag_specifications=tag_specifications,
            tags=lt_config.tags,
            user_data=lt_config.user_data,
            vpc_security_group_ids=lt_config.security_groups,
            metadata_options=LaunchTemplateMetadataOptionsArgs(
                http_endpoint="enabled",
                http_tokens="optional",
                http_put_response_hop_limit=5,
                instance_metadata_tags="enabled",
            ),
            opts=resource_options,
        )

        # Loop through the tags to populate to asg instances
        asg_tags = [
            GroupTagArgs(
                key=key_name,
                value=key_value,
                propagate_at_launch=True,
            )
            for key_name, key_value in asg_config.aws_config.merged_tags(
                {"ami_id": lt_config.image_id}
            ).items()
        ]

        auto_scale_group_kwargs = {}
        if self.target_group:
            auto_scale_group_kwargs["target_group_arns"] = [self.target_group.arn]
        self.auto_scale_group = Group(
            resource_name_prefix + "auto-scale-group",
            desired_capacity=asg_config.desired_size,
            health_check_type=asg_config.health_check_type,
            instance_refresh=instance_refresh_policy,
            launch_template=GroupLaunchTemplateArgs(
                id=self.launch_template.id,
                version="$Latest",
            ),
            max_size=asg_config.max_size,
            min_size=asg_config.min_size,
            tags=asg_tags,
            vpc_zone_identifiers=asg_config.vpc_zone_identifiers,
            opts=resource_options,
            **auto_scale_group_kwargs,
        )
