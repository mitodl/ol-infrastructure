from enum import unique, Enum
from typing import List, Optional
from pulumi_aws.ec2.security_group import SecurityGroup
from pulumi_aws.lb.load_balancer import LoadBalancer
from pulumi import Output

from pydantic.types import PositiveInt
from ol_infrastructure.lib.aws.ecs.fargate_service_config import ( 
    DeploymentControllerTypes, 
    LaunchTypes
)

from ol_infrastructure.lib.aws.ecs.task_definition_config import OLFargateTaskDefinitionConfig
from ol_infrastructure.lib.ol_types import AWSBase

from pulumi_aws.lb import (
    LoadBalancerAccessLogsArgs,
    ListenerDefaultActionArgs
)

from pulumi_aws.ecs import (
    Cluster
)

@unique
class Protocol(str, Enum):
    http = "HTTP"
    https = "HTTPS"

@unique
class Route53RecordType(str, Enum):
    alias = "ALIAS"
    cname = "CNAME"
    none = "NONE"

class OLApplicationLoadBalancedFargateConfig(AWSBase):

    """Name for component resource"""
    name: str

    """Name of service"""
    service_name: str

    """Block for configuring access logs written to S3 bucket"""
    access_log: Optional[LoadBalancerAccessLogsArgs] = None

    """Determines whether ECS Fargate service will have public IP or not"""
    assign_public_ip: bool = False    

    """If enabled, circuit breaker will automatically roll Service back to last successful deployment, if error occurs during deployment"""
    deployment_circuit_breaker_enabled: bool = False

    """ECS cluster that will be parent of ECS Service. Will be created if not provided"""
    cluster: Optional[Cluster] = None

    """Desired count for number of tasks on ECS Service"""
    desired_count: PositiveInt = PositiveInt(1)

    """Domain name for the load balancer. Eg- api.some-service.com"""
    domain_name: Optional[str]

    """Route 53 hosted zone. Eg- some-service.com"""
    zone_name: Optional[str] = None

    """Seconds to ignore failing load balancer health checks on newly created tasks"""
    health_check_grace_period_seconds: PositiveInt = PositiveInt(60)

    """Port load balancer listener will be available on. HTTP defaults to 80, HTTPS defaults to 443"""
    listener_port: Optional[PositiveInt]

    """Pre-built load balancer to use, if needed"""
    load_balancer: Optional[LoadBalancer] = None

    """Name of load balancer"""
    load_balancer_name: str

    """Max amount, as percentage, of running tasks that can run during a deployment"""
    deployment_max_percent: PositiveInt = PositiveInt(100)

    """Minimum amount, as percentage, of running and healthy tasks required during a deployment"""
    deployment_min_percent: PositiveInt = PositiveInt(50)

    """If true, load balancer will be internal; not public"""
    internal: bool = False

    """Whether or not listener will accept requests from all IP addresses"""
    listener_open_to_all_traffic: bool = True

    """If enabled, ECS Managed Tags will be enabled for tasks within service"""
    enable_ecs_managed_tags: bool = False

    """
    Protocol used for clients connecting to load balancer
    NOTE: if certificate is present, HTTPS will be enforced
    """
    load_balancer_protocol: Protocol = Protocol.http

    """Determines whether load balancer will be public or not"""
    public_load_balancer: bool = False

    """Defines type of route 53 record to be used if hosted zone is utilized"""
    route53_record_type: Route53RecordType = Route53RecordType.alias

    """Defines whether HTTP connections will automatically be redirected to HTTPs (80 -> 443)"""
    redirect_http: bool = False

    """Protocol for connections from load balancer to ECS targets. NOTE: To support HTTPs an additional SSL certificate is required"""
    target_protocol: Protocol = Protocol.http

    """Task Definition(s) to be used with ECS Service"""
    task_definition_config: OLFargateTaskDefinitionConfig

    """Type of IP address used by subnets, for the load balancer"""
    ip_address_type: str = "ipv4"

    """List of security group ids to be assigned to Fargate Service"""
    security_groups: List[SecurityGroup] = None

    """List of subnet ids to attach to load balancer"""
    subnets: List[str] = None

    """Name of ECS Fargate service"""
    service_name: Optional[str]

    """VPC service will be deployed into. Service and tasks will be deployed into public subnets, from this VPC"""
    vpc_id: Output[str]

    """List of action blocks for listener"""
    default_actions: Optional[List[ListenerDefaultActionArgs]] = None

    """Type of Deployment Controller used for service and tasks. Only ECS supported"""
    _deployment_controller: DeploymentControllerTypes = DeploymentControllerTypes.ecs

    """Lastest Fargate version will always be used"""
    _fargate_platform_version: str = "latest"

    """Launch type for service and tasks. Only FARGATE is supported"""
    _launch_type: LaunchTypes = LaunchTypes.fargate

    """Defines what type of load balancer to use. Always set to 'application'"""
    _load_balancer_type: str = "application"

    class Config:  # noqa: WPS431, D106
        arbitrary_types_allowed = True
