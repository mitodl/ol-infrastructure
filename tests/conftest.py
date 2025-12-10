"""Shared pytest fixtures for Pulumi infrastructure tests.

This module provides common fixtures and utilities for testing Pulumi
infrastructure code across the ol-infrastructure repository.
"""

import pulumi
import pytest


@pytest.fixture(scope="session")
def pulumi_mocks():
    """Set up Pulumi mocks for unit tests.

    This fixture configures Pulumi's mocking system. Resources created during
    tests will use mocked responses instead of making actual API calls.

    Note: Individual test classes can override this by calling
    pulumi.runtime.set_mocks() with custom mocks in their own fixtures.

    Returns:
        PulumiMocks: The mock class instance.
    """

    class PulumiMocks(pulumi.runtime.Mocks):
        """Mock implementation for Pulumi resources."""

        def new_resource(self, args: pulumi.runtime.MockResourceArgs):
            """Mock resource creation.

            Args:
                args: Arguments passed to resource constructor.

            Returns:
                tuple: (resource_id, resource_inputs) where resource_id is
                       the mocked ID and resource_inputs are the echoed inputs.
            """
            return [args.name + "_id", args.inputs]

        def call(self, args: pulumi.runtime.MockCallArgs):
            """Mock data source calls.

            Args:
                args: Arguments passed to data source function.

            Returns:
                dict: Mocked response data for the data source.
            """
            # Mock common AWS data sources
            if args.token == "aws:ec2/getVpc:getVpc":  # noqa: S105
                return {
                    "id": "vpc-12345678",
                    "cidrBlock": "10.0.0.0/16",
                    "enableDnsHostnames": True,
                    "enableDnsSupport": True,
                }
            if args.token == "aws:ec2/getAmi:getAmi":  # noqa: S105
                return {
                    "id": "ami-abc12345",
                    "imageId": "ami-abc12345",
                    "name": "test-ami",
                }
            if args.token == "aws:ec2/getSubnets:getSubnets":  # noqa: S105
                return {
                    "ids": ["subnet-11111111", "subnet-22222222", "subnet-33333333"],
                }
            if args.token == "aws:route53/getZone:getZone":  # noqa: S105
                return {
                    "id": "Z1234567890ABC",
                    "name": "example.com",
                    "zoneId": "Z1234567890ABC",
                }

            # Default empty response
            return {}

    # Don't set mocks automatically - let tests control when to set them
    # Tests can call: pulumi.runtime.set_mocks(pulumi_mocks)
    return PulumiMocks


@pytest.fixture
def aws_region():
    """Return default AWS region for tests.

    Returns:
        str: AWS region identifier.
    """
    return "us-east-1"


@pytest.fixture
def mock_vpc_id():
    """Mock VPC ID for tests.

    Returns:
        str: Mocked VPC identifier.
    """
    return "vpc-12345678"


@pytest.fixture
def mock_subnet_ids():
    """Mock subnet IDs for tests.

    Returns:
        list[str]: List of mocked subnet identifiers.
    """
    return ["subnet-11111111", "subnet-22222222", "subnet-33333333"]


@pytest.fixture
def mock_security_group_id():
    """Mock security group ID for tests.

    Returns:
        str: Mocked security group identifier.
    """
    return "sg-12345678"


@pytest.fixture
def mock_tags():
    """Return standard tags for test resources.

    Returns:
        dict: Dictionary of common resource tags.
    """
    return {
        "Environment": "test",
        "Owner": "platform-engineering",
        "Project": "ol-infrastructure",
        "ManagedBy": "pulumi",
    }
