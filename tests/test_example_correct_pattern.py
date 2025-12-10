"""Example unit test for Pulumi components using correct testing pattern.

This example demonstrates the proper way to test Pulumi resources in Python:
1. Define mocks at module level
2. Set mocks BEFORE importing infrastructure code
3. Use @pulumi.runtime.test decorator
4. Return Output.apply() from test functions

Reference: https://github.com/pulumi/examples/tree/master/testing-unit-py

Note: With Python 3.14+, Pulumi's set_mocks() requires an event loop. This is
handled automatically by pytest when tests run, but may cause issues if you
try to import this module directly. For production tests, import your
infrastructure code from a separate module.
"""

import asyncio

import pulumi
from pulumi_aws import ec2

# Python 3.14+ compatibility: ensure event loop exists for set_mocks()
try:
    asyncio.get_event_loop()
except RuntimeError:
    asyncio.set_event_loop(asyncio.new_event_loop())


class ExampleMocks(pulumi.runtime.Mocks):
    """Mock implementation for testing."""

    def new_resource(self, args: pulumi.runtime.MockResourceArgs):
        """Mock resource creation.

        Returns resource ID and outputs. Add specific outputs for certain
        resource types as needed.
        """
        outputs = args.inputs

        # Add mock outputs for EC2 instances
        if args.typ == "aws:ec2/instance:Instance":
            outputs = {
                **args.inputs,
                "publicIp": "203.0.113.12",
                "publicDns": "ec2-203-0-113-12.compute-1.amazonaws.com",
            }

        return [args.name + "_id", outputs]

    def call(self, args: pulumi.runtime.MockCallArgs):
        """Mock data source calls."""
        # Mock AMI lookup
        if args.token == "aws:ec2/getAmi:getAmi":  # noqa: S105
            return {
                "architecture": "x86_64",
                "id": "ami-0eb1f3cdeeb8eed2a",
            }
        return {}


# Set mocks BEFORE importing/creating resources
pulumi.runtime.set_mocks(ExampleMocks())


# Create example resources at module level
example_security_group = ec2.SecurityGroup(
    "example-sg",
    description="Example security group for testing",
    ingress=[
        # Allow HTTP from anywhere
        {
            "protocol": "tcp",
            "from_port": 80,
            "to_port": 80,
            "cidr_blocks": ["0.0.0.0/0"],
        },
        # Allow HTTPS from anywhere
        {
            "protocol": "tcp",
            "from_port": 443,
            "to_port": 443,
            "cidr_blocks": ["0.0.0.0/0"],
        },
    ],
    egress=[
        # Allow all outbound
        {
            "protocol": "-1",
            "from_port": 0,
            "to_port": 0,
            "cidr_blocks": ["0.0.0.0/0"],
        },
    ],
)

example_instance = ec2.Instance(
    "example-instance",
    instance_type="t3.micro",
    ami="ami-0eb1f3cdeeb8eed2a",  # Will be mocked
    vpc_security_group_ids=[example_security_group.id],
    tags={
        "Name": "example-instance",
        "Environment": "test",
        "ManagedBy": "pulumi",
    },
)


# Tests use @pulumi.runtime.test decorator
@pulumi.runtime.test
def test_security_group_has_description():
    """Verify security group has a description."""

    def check_description(args):
        urn, description = args
        assert description, f"Security group {urn} must have a description"
        assert len(description) > 0

    return pulumi.Output.all(
        example_security_group.urn, example_security_group.description
    ).apply(check_description)


@pulumi.runtime.test
def test_security_group_no_ssh_from_internet():
    """Verify SSH is not exposed to the internet."""

    def check_no_public_ssh(args):
        urn, ingress = args
        # Check if any rule opens port 22 to 0.0.0.0/0
        ssh_open = any(
            rule["from_port"] == 22
            and any(block == "0.0.0.0/0" for block in rule["cidr_blocks"])
            for rule in ingress
        )
        assert not ssh_open, f"Security group {urn} exposes SSH to the internet"

    return pulumi.Output.all(
        example_security_group.urn, example_security_group.ingress
    ).apply(check_no_public_ssh)


@pulumi.runtime.test
def test_instance_has_required_tags():
    """Verify instance has required tags."""

    def check_tags(args):
        urn, tags = args
        assert tags, f"Instance {urn} must have tags"
        assert "Name" in tags, f"Instance {urn} must have a Name tag"
        assert "Environment" in tags, f"Instance {urn} must have an Environment tag"
        assert "ManagedBy" in tags, f"Instance {urn} must have a ManagedBy tag"

    return pulumi.Output.all(example_instance.urn, example_instance.tags).apply(
        check_tags
    )


@pulumi.runtime.test
def test_instance_type():
    """Verify instance uses correct instance type."""

    def check_instance_type(args):
        urn, instance_type = args
        assert instance_type == "t3.micro", f"Expected t3.micro, got {instance_type}"

    return pulumi.Output.all(
        example_instance.urn, example_instance.instance_type
    ).apply(check_instance_type)


@pulumi.runtime.test
def test_instance_uses_security_group():
    """Verify instance is associated with security group."""

    def check_security_groups(args):
        urn, sg_ids = args
        assert sg_ids, f"Instance {urn} must have security groups"
        assert len(sg_ids) > 0, f"Instance {urn} must have at least one security group"

    return pulumi.Output.all(
        example_instance.urn, example_instance.vpc_security_group_ids
    ).apply(check_security_groups)
