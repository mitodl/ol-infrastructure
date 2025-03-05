from typing import Optional

from pulumi import ComponentResource, ResourceOptions, StackReference
from pulumi_aws import ec2

from ol_infrastructure.lib.aws.eks_helper import (
    default_psg_egress_args,
    get_default_psg_ingress_args,
)
from ol_infrastructure.lib.ol_types import AWSBase
from ol_infrastructure.lib.pulumi_helper import parse_stack

stack_info = parse_stack()
env_name = f"{stack_info.env_prefix}-{stack_info.env_suffix}"
network_stack = StackReference(f"infrastructure.aws.network.{stack_info.name}")
aws_config = AWSBase(
    tags={
        "OU": "applications",
        "Environment": f"{env_name}",
    }
)


class OLAppSecurityGroup(ComponentResource):
    """MIT OL security group component"""

    def __init__(
        self,
        app_name: str,
        target_vpc_name: str,
        opts: Optional[ResourceOptions] = None,
    ):
        super().__init__(
            "ol:infrastructure:aws:OLAppSecurityGroup",
            app_name,
            None,
            opts,
        )

        target_vpc = network_stack.require_output(target_vpc_name)
        ################################################
        # Application security group
        # Needs to happen ebfore the database security group is created
        k8s_pod_subnet_cidrs = target_vpc["k8s_pod_subnet_cidrs"]
        self.application_security_group = ec2.SecurityGroup(
            f"{app_name}-application-security-group-{stack_info.env_suffix}",
            name=f"{app_name}-application-security-group-{stack_info.env_suffix}",
            description=f"Access control for the {app_name} application pods.",
            # allow all egress traffic
            egress=default_psg_egress_args,
            ingress=get_default_psg_ingress_args(
                k8s_pod_subnet_cidrs=k8s_pod_subnet_cidrs,
            ),
            vpc_id=target_vpc["id"],
            tags=aws_config.tags,
        )
