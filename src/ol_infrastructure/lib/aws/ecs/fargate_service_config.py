from enum import Enum, unique
from typing import Optional, List

from pulumi import Output, log

from pulumi_aws.ecs import (
    Cluster, 
    ServiceLoadBalancerArgs,
    ServiceNetworkConfigurationArgs,
    ServiceDeploymentControllerArgs
)

from pulumi_aws.iam import Role

from pulumi_aws.ec2 import (
    SecurityGroup,
    get_subnet_ids
)

from pydantic import PositiveInt

from ol_infrastructure.lib.aws.ecs.task_definition_config import OLFargateTaskDefinitionConfig
from ol_infrastructure.lib.ol_types import AWSBase

@unique
class DeploymentControllerTypes(str, Enum):
    ecs = "ECS"
    code_deploy = "CODE_DEPLOY"
    external = "EXTERNAL"

@unique 
class LaunchTypes(str, Enum):
    fargate = "FARGATE"
    ec2 = "EC2"
    external = "EXTERNAL"

class OLFargateServiceConfig(AWSBase):
    """Configuration for constructing an ECS Fargate Service"""

    """base name for all resources"""
    service_name: str

    """ECS cluster that will be parent of ECS Service. Will be created if not provided"""
    cluster: Optional[Cluster]

    """Determines whether ECS Fargate service will have public IP or not. Defaults to true"""
    assign_public_ip: bool = True 

    """IAM Role for ECS Service to use for Load Balancer communication"""
    service_role: Optional[Role]

    """Desired count for number of tasks on ECS Service"""
    desired_count: PositiveInt = PositiveInt(1)

    """Max amount, as percentage, of running tasks that can run during a deployment"""
    deployment_max_percent: PositiveInt = PositiveInt(100)

    """Minimum amount, as percentage, of running and healthy tasks required during a deployment"""
    deployment_min_percent: PositiveInt = PositiveInt(50)

    """Seconds to ignore failing load balancer health checks on newly created tasks. Only applies when LB exists"""
    health_check_grace_period_seconds: PositiveInt = PositiveInt(60)

    """If enabled, circuit breaker will automatically roll Service back to last successful deployment, if error occurs during deployment"""
    deployment_circuit_breaker_enabled: bool = False

    """If enabled, ECS Managed Tags will be enabled for tasks within service"""
    enable_ecs_managed_tags: bool = False

    """VPC service will be deployed into. Service and tasks will be deployed into public subnets, from this VPC"""
    vpc_id: Output[str]

    """Security groups associated with the service and tasks"""
    security_groups: List[SecurityGroup]

    """Force a new task deploymennt of the service"""
    force_new_deployment: bool = False

    """Task Definition(s) to be used with ECS Service"""
    task_definition_config: OLFargateTaskDefinitionConfig

    """Type of Deployment Controller used for service and tasks. Only ECS supported"""
    _deployment_controller: DeploymentControllerTypes = DeploymentControllerTypes.ecs

    """Lastest Fargate version will always be used"""
    _fargate_platform_version: str = "LATEST"

    """Launch type for service and tasks. Only FARGATE is supported"""
    _launch_type: LaunchTypes = LaunchTypes.fargate

    """Load balancer configuration that will be used to attach containers to target groups"""
    load_balancer_configuration: Optional[List[ServiceLoadBalancerArgs]] = None

    """Retrieve all subnets from the provided VPC (vpc id). NOTE: No filtering is made upon subnets"""
    def get_service_network_configuration(self) -> ServiceNetworkConfigurationArgs:
        log.debug(f"retrieving all subnets from VPC '{self.vpc_id}'")

        subnets = get_subnet_ids(
            vpc_id=self.vpc_id,
        )

        log.debug(f"assign public IP addresses is set to {self.assign_public_ip}")

        return ServiceNetworkConfigurationArgs(
            subnets=subnets.ids,
            assign_public_ip=self.assign_public_ip,
            security_groups=[
                group.id
                for group in self.security_groups
            ]
        )

    def get_deployment_controller(self) -> ServiceDeploymentControllerArgs:
        log.debug(f"ECS deployment controller type is {self._deployment_controller}")

        return ServiceDeploymentControllerArgs(
            type=self._deployment_controller
        )

    class Config:  # noqa: WPS431, D106
        arbitrary_types_allowed = True