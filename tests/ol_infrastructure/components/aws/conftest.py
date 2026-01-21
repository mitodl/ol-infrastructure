"""Fixtures for AWS component tests."""

import pulumi
import pytest


class AutoScaleGroupMocks(pulumi.runtime.Mocks):
    """Mock implementation for testing ASG components."""

    def new_resource(self, args: pulumi.runtime.MockResourceArgs):
        """Mock resource creation."""
        outputs = args.inputs

        # Mock outputs for specific resource types
        if args.typ == "aws:ec2/launchTemplate:LaunchTemplate":
            outputs = {
                **args.inputs,
                "id": f"{args.name}_id",
                "latestVersion": 1,
                "arn": (
                    f"arn:aws:ec2:us-east-1:123456789012:launch-template/{args.name}"
                ),
            }
        elif args.typ == "aws:autoscaling/group:Group":
            outputs = {
                **args.inputs,
                "id": f"{args.name}_id",
                "arn": (
                    f"arn:aws:autoscaling:us-east-1:123456789012:"
                    f"autoScalingGroup:{args.name}"
                ),
            }
        elif args.typ == "aws:lb/targetGroup:TargetGroup":
            outputs = {
                **args.inputs,
                "id": f"{args.name}_id",
                "arn": (
                    f"arn:aws:elasticloadbalancing:us-east-1:123456789012:"
                    f"targetgroup/{args.name}/50dc6c495c0c9188"
                ),
            }
        elif args.typ == "aws:lb/loadBalancer:LoadBalancer":
            outputs = {
                **args.inputs,
                "id": f"{args.name}_id",
                "arn": (
                    f"arn:aws:elasticloadbalancing:us-east-1:123456789012:"
                    f"loadbalancer/app/{args.name}/50dc6c495c0c9188"
                ),
                "dnsName": f"{args.name}.us-east-1.elb.amazonaws.com",
            }
        elif args.typ == "aws:lb/listener:Listener":
            outputs = {
                **args.inputs,
                "id": f"{args.name}_id",
                "arn": (
                    f"arn:aws:elasticloadbalancing:us-east-1:123456789012:"
                    f"listener/app/{args.name}/50dc6c495c0c9188/f2f7dc8efc522ab2"
                ),
            }
        elif args.typ == "aws:ec2/securityGroup:SecurityGroup":
            outputs = {
                **args.inputs,
                "id": f"{args.name}_id",
                "arn": (
                    f"arn:aws:ec2:us-east-1:123456789012:security-group/{args.name}"
                ),
            }

        # Add _remote attribute to prevent AttributeError in ComponentResource
        # dependency resolution
        return [args.name + "_id", outputs]

    def call(self, args: pulumi.runtime.MockCallArgs):
        """Mock data source calls."""
        if args.token == "aws:acm/getCertificate:getCertificate":  # noqa: S105
            return {
                "arn": (
                    "arn:aws:acm:us-east-1:123456789012:certificate/"
                    "12345678-1234-1234-1234-123456789012"
                )
            }
        if args.token == "pulumi:pulumi:StackReference":  # noqa: S105
            return {
                "outputs": {
                    "kms_ec2_ebs_key": {
                        "arn": (
                            "arn:aws:kms:us-east-1:123456789012:key/"
                            "12345678-1234-1234-1234-123456789012"
                        )
                    }
                }
            }
        return {}


@pytest.fixture(autouse=True)
def auto_scale_group_mocks():
    """Set up AutoScaleGroupMocks for ASG tests.

    This fixture ensures that proper mocks are set up before each test
    and prevents cross-test pollution.
    """
    # Set mocks for this test
    pulumi.runtime.set_mocks(AutoScaleGroupMocks())
    # Mocks are cleaned up after test
