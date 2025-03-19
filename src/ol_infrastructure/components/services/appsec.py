from typing import Optional

from pulumi import ComponentResource, ResourceOptions, StackReference
from pulumi_aws import ec2
from pydantic import BaseModel, field_validator

from ol_infrastructure.lib.aws.eks_helper import (
    default_psg_egress_args,
    get_default_psg_ingress_args,
)
from ol_infrastructure.lib.ol_types import AWSBase
from ol_infrastructure.lib.pulumi_helper import parse_stack

stack_info = parse_stack()
env_name = f"{stack_info.env_prefix}-{stack_info.env_suffix}"


class OLAppSecurityGroupConfig(BaseModel):
    app_name: str
    target_vpc_name: str
    app_ou: str

    @field_validator("target_vpc_name")
    @classmethod
    def validate_target_vpc_name(cls, target_vpc_name: str) -> str:
        if not target_vpc_name.endswith("_vpc"):
            target_vpc_name += "_vpc"
        return target_vpc_name



class OLAppSecurityGroup(ComponentResource):
    """MIT OL security group component"""

    def __init__(
        self,
        app_security_group_config: OLAppSecurityGroupConfig,
        opts: Optional[ResourceOptions] = None,
    ):
        self.app_security_group_config: OLAppSecurityGroupConfig = (
            app_security_group_config
        )
        self.aws_config: AWSBase = AWSBase(
            tags={
                "OU": self.app_security_group_config.app_ou,
                "Environment": f"{env_name}",
            }
        )

        super().__init__(
            "ol:infrastructure:aws:OLAppSecurityGroup",
            f"{self.app_security_group_config.app_name}-security-group",
            None,
            opts,
        )

        # We do this here rather than at the top because we need a unique
        # identifier for the name.
        security_group_network_stack = StackReference(
            name=f"security_group_network_stack_reference_{self.app_security_group_config.app_name}_{self.app_security_group_config.app_ou}",
            stack_name=f"infrastructure.aws.network.{stack_info.name}",
        )
        target_vpc = security_group_network_stack.require_output(
            self.app_security_group_config.target_vpc_name
        )
        ################################################
        # Application security group
        # Needs to happen ebfore the database security group is created
        k8s_pod_subnet_cidrs = target_vpc["k8s_pod_subnet_cidrs"]
        self.application_security_group = ec2.SecurityGroup(
            f"{self.app_security_group_config.app_name}-application-security-group-{stack_info.env_suffix}",
            name=f"{self.app_security_group_config.app_name}-application-security-group-{stack_info.env_suffix}",
            description=f"""
                        Access control for the
                        {self.app_security_group_config.app_name} application
                        pods.
                        """,
            # allow all egress traffic
            egress=default_psg_egress_args,
            ingress=get_default_psg_ingress_args(
                k8s_pod_subnet_cidrs=k8s_pod_subnet_cidrs,
            ),
            vpc_id=target_vpc["id"],
            tags=self.aws_config.tags,
        )
