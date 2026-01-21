"""Tests for OLAutoScaling component and spot instance configuration.

This test validates:
1. Launch templates can be configured with spot instances
2. Spot instance options are properly applied
3. On-demand instances work by default (backwards compatibility)
"""

import asyncio
import os
from unittest import mock

# Set AWS environment variables before importing boto3-dependent modules
os.environ["AWS_DEFAULT_REGION"] = "us-east-1"
os.environ["AWS_REGION"] = "us-east-1"
os.environ["AWS_ACCESS_KEY_ID"] = "testing"
os.environ["AWS_SECRET_ACCESS_KEY"] = (
    "testing"  # pragma: allowlist secret  # noqa: S105
)

# Mock the is_valid_instance_type function globally to avoid AWS API calls
_mock_patcher = mock.patch(
    "ol_infrastructure.lib.aws.ec2_helper.is_valid_instance_type", return_value=True
)
_mock_patcher.start()

import pulumi  # noqa: E402

# Python 3.14+ compatibility: ensure event loop exists for set_mocks()
try:
    asyncio.get_event_loop()
except RuntimeError:
    asyncio.set_event_loop(asyncio.new_event_loop())

# Import the component (mocks will be set by conftest fixture)
from ol_infrastructure.components.aws.auto_scale_group import (  # noqa: E402
    BlockDeviceMapping,
    OLAutoScaleGroupConfig,
    OLAutoScaling,
    OLLaunchTemplateConfig,
    OLLoadBalancerConfig,
    OLTargetGroupConfig,
    SpotInstanceOptions,
    TagSpecification,
)


# Helper function to create default ASG components
def create_default_asg():
    """Create default on-demand ASG for testing."""
    mock_sg_id = pulumi.Output.from_input("sg-12345678")
    lt_config = OLLaunchTemplateConfig(
        block_device_mappings=[BlockDeviceMapping(volume_size=25, encrypted=False)],
        image_id="ami-12345678",
        instance_profile_arn="arn:aws:iam::123456789012:instance-profile/test",
        instance_type="t3.medium",
        security_groups=[mock_sg_id],
        tag_specifications=[
            TagSpecification(
                resource_type="instance",
                tags={"Name": "test-instance", "Environment": "test"},
            )
        ],
        user_data=None,
        tags={"Name": "test-lt", "OU": "operations", "Environment": "test"},
    )
    asg_config = OLAutoScaleGroupConfig(
        asg_name="test-default-asg",
        desired_size=2,
        min_size=1,
        max_size=3,
        vpc_zone_identifiers=pulumi.Output.from_input(["subnet-1", "subnet-2"]),
        tags={"Name": "test-asg", "OU": "operations", "Environment": "test"},
    )
    return OLAutoScaling(asg_config=asg_config, lt_config=lt_config)


def create_spot_asg():
    """Create spot instance ASG for testing."""
    mock_sg_id = pulumi.Output.from_input("sg-12345678")
    lt_config = OLLaunchTemplateConfig(
        block_device_mappings=[BlockDeviceMapping(volume_size=25, encrypted=False)],
        image_id="ami-12345678",
        instance_profile_arn="arn:aws:iam::123456789012:instance-profile/test",
        instance_type="t3.medium",
        security_groups=[mock_sg_id],
        tag_specifications=[
            TagSpecification(
                resource_type="instance",
                tags={"Name": "test-spot-instance", "Environment": "test"},
            )
        ],
        user_data=None,
        use_spot_instances=True,
        tags={"Name": "test-spot-lt", "OU": "operations", "Environment": "test"},
    )
    asg_config = OLAutoScaleGroupConfig(
        asg_name="test-spot-asg",
        desired_size=2,
        min_size=1,
        max_size=3,
        vpc_zone_identifiers=pulumi.Output.from_input(["subnet-1", "subnet-2"]),
        tags={"Name": "test-spot-asg", "OU": "operations", "Environment": "test"},
    )
    return OLAutoScaling(asg_config=asg_config, lt_config=lt_config)


def create_custom_spot_asg():
    """Create spot instance ASG with custom options for testing."""
    mock_sg_id = pulumi.Output.from_input("sg-12345678")
    lt_config = OLLaunchTemplateConfig(
        block_device_mappings=[BlockDeviceMapping(volume_size=25, encrypted=False)],
        image_id="ami-12345678",
        instance_profile_arn="arn:aws:iam::123456789012:instance-profile/test",
        instance_type="t3.medium",
        security_groups=[mock_sg_id],
        tag_specifications=[
            TagSpecification(
                resource_type="instance",
                tags={"Name": "test-custom-spot-instance", "Environment": "test"},
            )
        ],
        user_data=None,
        use_spot_instances=True,
        spot_options=SpotInstanceOptions(
            max_price="0.05",
            spot_instance_type="one-time",
            instance_interruption_behavior="terminate",
        ),
        tags={"Name": "test-custom-spot-lt", "OU": "operations", "Environment": "test"},
    )
    asg_config = OLAutoScaleGroupConfig(
        asg_name="test-custom-spot-asg",
        desired_size=2,
        min_size=1,
        max_size=3,
        vpc_zone_identifiers=pulumi.Output.from_input(["subnet-1", "subnet-2"]),
        tags={
            "Name": "test-custom-spot-asg",
            "OU": "operations",
            "Environment": "test",
        },
    )
    return OLAutoScaling(asg_config=asg_config, lt_config=lt_config)


def create_spot_asg_with_lb():
    """Create spot instance ASG with load balancer for testing."""
    mock_sg_id = pulumi.Output.from_input("sg-12345678")
    lt_config = OLLaunchTemplateConfig(
        block_device_mappings=[BlockDeviceMapping(volume_size=25, encrypted=False)],
        image_id="ami-12345678",
        instance_profile_arn="arn:aws:iam::123456789012:instance-profile/test",
        instance_type="t3.medium",
        security_groups=[mock_sg_id],
        tag_specifications=[
            TagSpecification(
                resource_type="instance",
                tags={"Name": "test-spot-lb-instance", "Environment": "test"},
            )
        ],
        user_data=None,
        use_spot_instances=True,
        tags={"Name": "test-spot-lb-lt", "OU": "operations", "Environment": "test"},
    )
    asg_config = OLAutoScaleGroupConfig(
        asg_name="test-spot-lb-asg",
        desired_size=2,
        min_size=1,
        max_size=3,
        vpc_zone_identifiers=pulumi.Output.from_input(["subnet-1", "subnet-2"]),
        tags={"Name": "test-spot-lb-asg", "OU": "operations", "Environment": "test"},
    )
    tg_config = OLTargetGroupConfig(
        vpc_id="vpc-12345",
        port=443,
        protocol="HTTPS",
        tags={"Name": "test-spot-lb-tg", "OU": "operations", "Environment": "test"},
    )
    lb_config = OLLoadBalancerConfig(
        subnets=pulumi.Output.from_input(["subnet-1", "subnet-2"]),
        security_groups=[mock_sg_id],
        tags={"Name": "test-spot-lb", "OU": "operations", "Environment": "test"},
    )
    return OLAutoScaling(
        asg_config=asg_config,
        lt_config=lt_config,
        tg_config=tg_config,
        lb_config=lb_config,
    )


# Unit tests using @pulumi.runtime.test decorator
@pulumi.runtime.test
def test_default_launch_template_no_spot_instances():
    """Verify default launch template does not configure spot instances."""
    asg = create_default_asg()

    def check_no_spot(args):
        urn, instance_market_options = args
        # instance_market_options should be None/undefined for on-demand instances
        assert instance_market_options is None or instance_market_options == {}, (
            f"Launch template {urn} should not have spot instance configuration"
        )

    return pulumi.Output.all(
        asg.launch_template.urn,
        asg.launch_template.instance_market_options,
    ).apply(check_no_spot)


@pulumi.runtime.test
def test_spot_launch_template_has_market_options():
    """Verify spot instance launch template has market options configured."""
    asg = create_spot_asg()

    def check_spot_configured(args):
        urn, instance_market_options = args
        assert instance_market_options is not None, (
            f"Launch template {urn} should have spot instance configuration"
        )
        assert instance_market_options.get("market_type") == "spot", (
            f"Launch template {urn} should have market type 'spot'"
        )

    return pulumi.Output.all(
        asg.launch_template.urn,
        asg.launch_template.instance_market_options,
    ).apply(check_spot_configured)


@pulumi.runtime.test
def test_spot_launch_template_default_options():
    """Verify spot instance with default options."""
    asg = create_spot_asg()

    def check_default_spot_options(args):
        urn, instance_market_options = args
        assert instance_market_options is not None
        spot_options = instance_market_options.get("spot_options", {})
        assert spot_options.get("spot_instance_type") == "one-time", (
            "Default spot instance type should be 'one-time'"
        )
        assert spot_options.get("instance_interruption_behavior") == "terminate", (
            "Default interruption behavior should be 'terminate'"
        )

    return pulumi.Output.all(
        asg.launch_template.urn,
        asg.launch_template.instance_market_options,
    ).apply(check_default_spot_options)


@pulumi.runtime.test
def test_spot_launch_template_custom_options():
    """Verify spot instance with custom options."""
    asg = create_custom_spot_asg()

    def check_custom_spot_options(args):
        urn, instance_market_options = args
        assert instance_market_options is not None
        spot_options = instance_market_options.get("spot_options", {})
        assert spot_options.get("max_price") == "0.05", (
            "Custom max price should be '0.05'"
        )
        assert spot_options.get("spot_instance_type") == "one-time", (
            "Custom spot instance type should be 'one-time'"
        )
        assert spot_options.get("instance_interruption_behavior") == "terminate", (
            "Custom interruption behavior should be 'terminate'"
        )

    return pulumi.Output.all(
        asg.launch_template.urn,
        asg.launch_template.instance_market_options,
    ).apply(check_custom_spot_options)


@pulumi.runtime.test
def test_asg_created_with_launch_template():
    """Verify auto scaling group is created with launch template."""
    asg = create_default_asg()

    def check_asg_lt(args):
        urn, launch_template = args
        assert launch_template is not None, (
            f"Auto scaling group {urn} should have a launch template"
        )
        assert "id" in launch_template, "Launch template should have an ID"

    return pulumi.Output.all(
        asg.auto_scale_group.urn,
        asg.auto_scale_group.launch_template,
    ).apply(check_asg_lt)


@pulumi.runtime.test
def test_spot_asg_with_load_balancer():
    """Verify spot instance ASG works with load balancer."""
    asg = create_spot_asg_with_lb()

    def check_spot_with_lb(args):
        urn, target_group_arns = args
        assert target_group_arns is not None, (
            f"Spot instance ASG {urn} should be associated with target group"
        )
        assert len(target_group_arns) > 0, (
            f"Spot instance ASG {urn} should have at least one target group"
        )

    return pulumi.Output.all(
        asg.auto_scale_group.urn,
        asg.auto_scale_group.target_group_arns,
    ).apply(check_spot_with_lb)


@pulumi.runtime.test
def test_load_balancer_created():
    """Verify load balancer is created when configured."""
    asg = create_spot_asg_with_lb()

    def check_lb_created(args):
        urn, dns_name = args
        assert dns_name is not None, f"Load balancer {urn} should have a DNS name"

    return pulumi.Output.all(
        asg.load_balancer.urn,
        asg.load_balancer.dns_name,
    ).apply(check_lb_created)
