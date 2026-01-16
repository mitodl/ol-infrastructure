from typing import Literal

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
    LaunchTemplateInstanceMarketOptionsArgs,
    LaunchTemplateInstanceMarketOptionsSpotOptionsArgs,
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
    TargetGroupStickinessArgs,
)
from pydantic import BaseModel, ConfigDict, NonNegativeInt, PositiveInt, field_validator

from bridge.lib.magic_numbers import (
    AWS_LOAD_BALANCER_NAME_MAX_LENGTH,
    AWS_TARGET_GROUP_NAME_MAX_LENGTH,
    DEFAULT_HTTP_PORT,
    DEFAULT_HTTPS_PORT,
)
from ol_infrastructure.lib.aws.ec2_helper import (
    DiskTypes,
    InstanceTypes,
    is_valid_instance_type,
)
from ol_infrastructure.lib.ol_types import AWSBase
from ol_infrastructure.lib.pulumi_helper import parse_stack


class BlockDeviceMapping(BaseModel):
    """Container for describing a block device mapping for an EC2 instance/launch template"""  # noqa: E501

    delete_on_termination: bool = True
    device_name: str = "/dev/xvda"
    encrypted: bool = True
    kms_key_arn: pulumi.Output[str] | str | None = None
    iops: int | None = None
    throughput: int | None = None
    volume_size: PositiveInt = PositiveInt(25)
    volume_type: DiskTypes = DiskTypes.ssd
    model_config = ConfigDict(arbitrary_types_allowed=True)


class TagSpecification(BaseModel):
    """Container for describing a tag specification for an EC2 launch template"""

    resource_type: str
    tags: pulumi.Output[dict[str, str]] | dict[str, str]
    model_config = ConfigDict(arbitrary_types_allowed=True)


class SpotInstanceOptions(BaseModel):
    """Container for describing spot instance configuration for a launch template."""

    max_price: str | None = (
        None  # Maximum price per hour. If None, uses on-demand price
    )
    spot_instance_type: Literal["one-time", "persistent"] = "one-time"
    instance_interruption_behavior: Literal["hibernate", "stop", "terminate"] = (
        "terminate"
    )
    model_config = ConfigDict(arbitrary_types_allowed=True)

    @field_validator("spot_instance_type")
    @classmethod
    def is_valid_spot_instance_type(cls, spot_instance_type: str) -> str:
        """Validate spot instance type is one of the accepted values."""
        if spot_instance_type not in ["one-time", "persistent"]:
            msg = f"spot_instance_type: {spot_instance_type} is not valid. Only 'one-time' or 'persistent' are accepted."  # noqa: E501
            raise ValueError(msg)
        return spot_instance_type

    @field_validator("instance_interruption_behavior")
    @classmethod
    def is_valid_interruption_behavior(cls, behavior: str) -> str:
        """Validate instance interruption behavior is one of the accepted values."""
        if behavior not in ["hibernate", "stop", "terminate"]:
            msg = f"instance_interruption_behavior: {behavior} is not valid. Only 'hibernate', 'stop', or 'terminate' are accepted."  # noqa: E501
            raise ValueError(msg)
        return behavior


class OLTargetGroupConfig(AWSBase):
    """Configuration object for defining a target group for use with a loadbalancer."""

    vpc_id: str | pulumi.Output[str]
    port: NonNegativeInt = NonNegativeInt(443)
    protocol: str = "HTTPS"
    stickiness: str | None = None

    health_check_enabled: bool = True
    health_check_healthy_threshold: PositiveInt = PositiveInt(2)
    health_check_interval: PositiveInt = PositiveInt(10)
    health_check_matcher: str = "200"
    health_check_path: str = "/"
    health_check_port: str = str(DEFAULT_HTTPS_PORT)
    health_check_protocol: str = "HTTPS"
    health_check_timeout: PositiveInt = PositiveInt(5)
    health_check_unhealthy_threshold: PositiveInt = PositiveInt(3)
    model_config = ConfigDict(arbitrary_types_allowed=True)

    @field_validator("stickiness")
    @classmethod
    def is_valid_stickiness(cls, stickiness: str) -> str:
        if stickiness and stickiness not in ["lb_cookie"]:
            msg = f"stickiness: {stickiness} is not valid. Only 'lb_cookie' is supported at this time."  # noqa: E501
            raise ValueError(msg)
        return stickiness


class OLLoadBalancerConfig(AWSBase):
    """Configuration object for defining a load balancer object for use with an autoscale group"""  # noqa: E501

    enable_http2: bool = True
    enable_insecure_http: bool = False
    internal: bool = False
    ip_address_type: str = "dualstack"
    load_balancer_type: str = "application"
    port: PositiveInt = PositiveInt(DEFAULT_HTTPS_PORT)
    security_groups: list[SecurityGroup | str | pulumi.Output[str]]
    subnets: pulumi.Output[str]

    idle_timeout_seconds: int | None = 60
    listener_use_acm: bool = True
    listener_cert_arn: str | pulumi.Output[str] | None = None
    listener_cert_domain: str = "*.odl.mit.edu"
    listener_protocol: str = "HTTPS"
    listener_action_type: str = "forward"
    model_config = ConfigDict(arbitrary_types_allowed=True)

    @field_validator("ip_address_type")
    @classmethod
    def is_valid_ip_address_type(cls, ip_address_type: str) -> str:
        if ip_address_type not in ["dualstack", "ipv4"]:
            msg = f"ip_address_type: {ip_address_type} is not valid. Only 'dualstack' and 'ipv4 are accepted'"  # noqa: E501
            raise ValueError(msg)
        return ip_address_type

    @field_validator("load_balancer_type")
    @classmethod
    def is_valid_load_balancer_type(cls, load_balancer_type: str) -> str:
        if load_balancer_type not in [
            "application",
            "gateway",
            "network",
        ]:
            msg = f"load_balancer_type: {load_balancer_type} is not valid. Only 'application', 'gateway', or 'network' are accepted"  # noqa: E501
            raise ValueError(msg)
        return load_balancer_type


class OLLaunchTemplateConfig(AWSBase):
    """Configuration Object for defining configuration needed to create a launch template."""  # noqa: E501

    block_device_mappings: list[BlockDeviceMapping]
    image_id: str
    instance_profile_arn: str | pulumi.Output[str]
    instance_type: str = InstanceTypes.burstable_medium
    key_name: str = "oldevops"
    security_groups: list[SecurityGroup | pulumi.Output]
    tag_specifications: list[TagSpecification]
    user_data: str | pulumi.Output[str] | None
    use_spot_instances: bool = False
    spot_options: SpotInstanceOptions | None = None
    model_config = ConfigDict(arbitrary_types_allowed=True)

    @field_validator("instance_type")
    @classmethod
    def is_valid_instance_type(cls, instance_type: str) -> str:
        if is_valid_instance_type(instance_type):
            return instance_type
        else:
            msg = (
                "The declared instance type is not a valid specifier. "
                "Refer to https://instances.vantage.sh/ to find a supported "
                "instance type."
            )
            raise ValueError(msg)


class OLAutoScaleGroupConfig(AWSBase):
    """Configuration Object for defining configuration needed to create an autoscale group."""  # noqa: E501

    asg_name: str
    desired_size: PositiveInt = PositiveInt(2)
    health_check_grace_period: NonNegativeInt = NonNegativeInt(0)
    health_check_type: Literal["ELB", "EC2"] = "ELB"
    max_size: PositiveInt = PositiveInt(2)
    min_size: PositiveInt = PositiveInt(1)
    vpc_zone_identifiers: pulumi.Output[str]

    instance_refresh_checkpoint_delay: PositiveInt = PositiveInt(3600)
    instance_refresh_checkpoint_percentages: list[NonNegativeInt] = []  # noqa: RUF012
    instance_refresh_warmup: PositiveInt = PositiveInt(health_check_grace_period)
    instance_refresh_min_healthy_percentage: NonNegativeInt = NonNegativeInt(50)
    instance_refresh_strategy: str = "Rolling"
    instance_refresh_triggers: list[str] = ["tag"]  # noqa: RUF012
    max_instance_lifetime_seconds: NonNegativeInt | None = 2592000  # 30 days
    model_config = ConfigDict(arbitrary_types_allowed=True)

    @field_validator("instance_refresh_strategy")
    @classmethod
    def is_valid_strategy(cls, strategy: str) -> str:
        if strategy != "Rolling":
            msg = "The only vaild instance refresh strategy is 'Rolling'"
            raise ValueError(msg)
        return strategy

    @field_validator("health_check_type")
    @classmethod
    def is_valid_healthcheck(cls, health_check_type: str) -> str:
        if health_check_type not in ["ELB", "EC2"]:
            msg = f"health_check_type: {health_check_type} is not valid. Only 'ELB' or 'EC2' are accepted."  # noqa: E501
            raise ValueError(msg)
        return health_check_type


class OLAutoScaling(pulumi.ComponentResource):
    """Component to create an autoscaling group with defaults as well as managed associated resources"""  # noqa: E501

    load_balancer: LoadBalancer = None
    listener: Listener = None
    target_group: TargetGroup = None
    auto_scale_group: Group = None
    launch_template: LaunchTemplate = None

    def __init__(  # noqa: C901, PLR0912, PLR0915
        self,
        asg_config: OLAutoScaleGroupConfig,
        lt_config: OLLaunchTemplateConfig,
        tg_config: OLTargetGroupConfig | None = None,
        lb_config: OLLoadBalancerConfig | None = None,
        opts: pulumi.ResourceOptions | None = None,
    ):
        super().__init__(
            "ol:infrastructure:aws:auto_scale_group:OLAutoScaleGroup",
            asg_config.asg_name,
            None,
            opts,
        )

        if bool(tg_config) != bool(lb_config):
            msg = "Both lb_config and tg_config must be provided if one is provided"
            raise ValueError(msg)

        # Shared attributes
        resource_options = pulumi.ResourceOptions(parent=self).merge(opts)
        resource_name_prefix = f"{asg_config.asg_name}-"

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

            target_group_stickiness = None
            if tg_config.stickiness:
                target_group_stickiness = TargetGroupStickinessArgs(
                    type=tg_config.stickiness,
                    enabled=True,
                )

            target_group_name = f"{resource_name_prefix}tg"[
                :AWS_TARGET_GROUP_NAME_MAX_LENGTH
            ].rstrip("-")
            self.target_group = TargetGroup(
                f"{resource_name_prefix}target-group",
                name=target_group_name,
                vpc_id=tg_config.vpc_id,
                port=tg_config.port,
                protocol=tg_config.protocol,
                health_check=target_group_healthcheck,
                stickiness=target_group_stickiness,
                opts=resource_options,
                tags=tg_config.tags,
            )

        # Create Load Balancer
        if lb_config:
            sg_ids = []
            for group in lb_config.security_groups:
                if isinstance(group, SecurityGroup):
                    sg_ids.append(group.id)
                else:
                    sg_ids.append(group)
            load_balancer_name = f"{resource_name_prefix}lb"[
                :AWS_LOAD_BALANCER_NAME_MAX_LENGTH
            ].rstrip("-")
            self.load_balancer = LoadBalancer(
                f"{resource_name_prefix}load-balancer",
                enable_http2=lb_config.enable_http2,
                internal=lb_config.internal,
                ip_address_type=lb_config.ip_address_type,
                load_balancer_type=lb_config.load_balancer_type,
                name=load_balancer_name,
                security_groups=sg_ids,
                subnets=lb_config.subnets,
                tags=lb_config.tags,
                opts=resource_options,
                idle_timeout=lb_config.idle_timeout_seconds or 60,
            )

            # Create Load Balancer Listener (requires load balancer + target group first)  # noqa: E501
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
                tags=lb_config.tags,
            )
            if lb_config.listener_use_acm:
                listener_args.certificate_arn = (
                    lb_config.listener_cert_arn
                    or get_certificate(
                        domain=lb_config.listener_cert_domain,
                        most_recent=True,
                        statuses=["ISSUED"],
                    ).arn
                )
            self.listener = Listener(
                f"{resource_name_prefix}load-balancer-listener",
                args=listener_args,
                opts=resource_options,
                tags=lb_config.tags,
            )

            if lb_config.enable_insecure_http:
                Listener(
                    f"{resource_name_prefix}load-balanancer-http-listener",
                    args=ListenerArgs(
                        load_balancer_arn=self.load_balancer.arn,
                        port=DEFAULT_HTTP_PORT,
                        protocol="HTTP",
                        default_actions=[
                            ListenerDefaultActionArgs(
                                type=lb_config.listener_action_type,
                                target_group_arn=self.target_group.arn,
                            )
                        ],
                    ),
                    opts=resource_options,
                    tags=lb_config.tags,
                )

        # Build list of block devices for launch template
        block_device_mappings = []
        stack_info = parse_stack()
        kms_stack = pulumi.StackReference(
            name=f"asg_kms_stack_reference_{asg_config.asg_name}",
            stack_name=f"infrastructure.aws.kms.{stack_info.name}",
        )
        for bdm in lt_config.block_device_mappings:
            if bdm.encrypted:
                kms_key_id = (
                    bdm.kms_key_arn
                    if bdm.kms_key_arn is not None
                    else kms_stack.require_output("kms_ec2_ebs_key")["arn"]
                )
            else:
                kms_key_id = None
            block_device_mapping = LaunchTemplateBlockDeviceMappingArgs(
                device_name=bdm.device_name,
                ebs=LaunchTemplateBlockDeviceMappingEbsArgs(
                    volume_size=bdm.volume_size,
                    volume_type=bdm.volume_type,
                    iops=bdm.iops,
                    throughput=bdm.throughput,
                    delete_on_termination=bdm.delete_on_termination,
                    encrypted=bdm.encrypted,
                    kms_key_id=kms_key_id,
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
        launch_template_kwargs = {
            "name_prefix": resource_name_prefix,
            "block_device_mappings": block_device_mappings,
            "iam_instance_profile": LaunchTemplateIamInstanceProfileArgs(
                arn=lt_config.instance_profile_arn,
            ),
            "image_id": lt_config.image_id,
            "instance_type": lt_config.instance_type,
            "key_name": lt_config.key_name,
            "tag_specifications": tag_specifications,
            "tags": lt_config.tags,
            "user_data": lt_config.user_data,
            "vpc_security_group_ids": lt_config.security_groups,
            "metadata_options": LaunchTemplateMetadataOptionsArgs(
                http_endpoint="enabled",
                http_tokens="optional",
                http_put_response_hop_limit=5,
                instance_metadata_tags="enabled",
            ),
            "opts": resource_options,
        }

        # Configure spot instances if requested
        if lt_config.use_spot_instances:
            spot_options_config = lt_config.spot_options or SpotInstanceOptions()
            spot_options_kwargs: dict[str, str] = {
                "spot_instance_type": spot_options_config.spot_instance_type,
                "instance_interruption_behavior": (
                    spot_options_config.instance_interruption_behavior
                ),
            }
            # Only set max_price if explicitly provided (None means use on-demand price)
            if spot_options_config.max_price is not None:
                spot_options_kwargs["max_price"] = spot_options_config.max_price

            launch_template_kwargs["instance_market_options"] = (
                LaunchTemplateInstanceMarketOptionsArgs(
                    market_type="spot",
                    spot_options=LaunchTemplateInstanceMarketOptionsSpotOptionsArgs(
                        **spot_options_kwargs
                    ),
                )
            )

        self.launch_template = LaunchTemplate(
            f"{resource_name_prefix}launch-template",
            **launch_template_kwargs,
        )

        # Loop through the tags to populate to asg instances
        asg_tags = [
            GroupTagArgs(
                key=key_name,
                value=key_value,
                propagate_at_launch=True,
            )
            for key_name, key_value in asg_config.merged_tags(
                {"ami_id": lt_config.image_id}
            ).items()
        ]

        auto_scale_group_kwargs = {}
        if self.target_group:
            auto_scale_group_kwargs["target_group_arns"] = [self.target_group.arn]
        self.auto_scale_group = Group(
            f"{resource_name_prefix}auto-scale-group",
            desired_capacity=asg_config.desired_size,
            health_check_type=asg_config.health_check_type,
            instance_refresh=instance_refresh_policy,
            launch_template=GroupLaunchTemplateArgs(
                id=self.launch_template.id,
                version="$Latest",
            ),
            max_size=asg_config.max_size,
            min_size=asg_config.min_size,
            max_instance_lifetime=asg_config.max_instance_lifetime_seconds,
            tags=asg_tags,
            vpc_zone_identifiers=asg_config.vpc_zone_identifiers,
            opts=resource_options,
            **auto_scale_group_kwargs,
        )
