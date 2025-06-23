"""Best practices for building Application Load Balanced ECS Fargate Services.

This includes:

- ECS Cluster
- ECS Service
- ECS Task Definition - Single image task or multiple images (sidecar)
- EC2 Security Group(s)
- EC2 Load Balancer
- EC2 Listener
- EC2 Target Group
- Route53 Zone (optional)
- ACM Certificate (optional)

Required On Input:
- VPC
- Subnets (Implicit)
"""

from enum import Enum, unique

import pulumi
from pulumi.resource import ResourceOptions
from pulumi_aws.acm import get_certificate
from pulumi_aws.ec2 import (
    SecurityGroup,
    SecurityGroupEgressArgs,
    SecurityGroupIngressArgs,
    get_subnet_ids,
)
from pulumi_aws.ecs import Cluster, ServiceLoadBalancerArgs
from pulumi_aws.lb import (
    Listener,
    ListenerDefaultActionArgs,
    ListenerDefaultActionRedirectArgs,
    LoadBalancer,
    LoadBalancerAccessLogsArgs,
    TargetGroup,
)
from pulumi_aws.route53 import Record, RecordAliasArgs, get_zone
from pydantic import ConfigDict, PositiveInt

from bridge.lib.magic_numbers import DEFAULT_HTTP_PORT, DEFAULT_HTTPS_PORT
from ol_infrastructure.components.aws.fargate_service import (
    DeploymentControllerTypes,
    LaunchTypes,
    OLFargateService,
    OLFargateServiceConfig,
)
from ol_infrastructure.lib.aws.ecs.task_definition_config import (
    OLFargateTaskDefinitionConfig,
)
from ol_infrastructure.lib.ol_types import AWSBase


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
    # Name for component resource
    name: str
    # Name of ECS Fargate service
    service_name: str
    # Block for configuring access logs written to S3 bucket
    access_log: LoadBalancerAccessLogsArgs | None = None
    # Determines whether ECS Fargate service will have public IP or not
    assign_public_ip: bool = False
    # If enabled, circuit breaker will automatically roll Service back to last
    # successful deployment, if error occurs during deployment
    deployment_circuit_breaker_enabled: bool = False
    # ECS cluster that will be parent of ECS Service. Will be created if not provided
    cluster: Cluster | None = None
    # Desired count for number of tasks on ECS Service
    desired_count: PositiveInt = PositiveInt(1)
    # Domain name for the load balancer. Eg- api.some-service.com
    domain_name: str | None
    # Route 53 hosted zone. Eg- some-service.com
    zone_name: str | None = None
    # Seconds to ignore failing load balancer health checks on newly created tasks
    health_check_grace_period_seconds: PositiveInt = PositiveInt(60)
    # Port load balancer listener will be available on. HTTP defaults to 80, HTTPS
    # defaults to 443
    listener_port: PositiveInt | None
    # Pre-built load balancer to use, if needed
    load_balancer: LoadBalancer | None = None
    # Name of load balancer
    load_balancer_name: str
    # Max amount, as percentage, of running tasks that can run during a deployment
    deployment_max_percent: PositiveInt = PositiveInt(100)
    # Minimum amount, as percentage, of running and healthy tasks required during a
    # deployment
    deployment_min_percent: PositiveInt = PositiveInt(50)
    # If true, load balancer will be internal; not public
    internal: bool = False
    # Whether or not listener will accept requests from all IP addresses
    listener_open_to_all_traffic: bool = True
    # If enabled, ECS Managed Tags will be enabled for tasks within service
    enable_ecs_managed_tags: bool = False
    # Protocol used for clients connecting to load balancer
    # NOTE: if certificate is present, HTTPS will be enforced
    load_balancer_protocol: Protocol = Protocol.http
    # Determines whether load balancer will be public or not
    public_load_balancer: bool = False
    # Defines type of route 53 record to be used if hosted zone is utilized
    route53_record_type: Route53RecordType = Route53RecordType.alias
    # Defines whether HTTP connections will automatically be redirected to HTTPs (80 ->
    # 443)
    redirect_http: bool = False
    # Protocol for connections from load balancer to ECS targets. NOTE: To support HTTPs
    # an additional SSL certificate is required
    target_protocol: Protocol = Protocol.http
    # Task Definition(s) to be used with ECS Service
    task_definition_config: OLFargateTaskDefinitionConfig
    # Type of IP address used by subnets, for the load balancer
    ip_address_type: str = "ipv4"
    # Security groups associated with the service and tasks
    security_groups: list[SecurityGroup]
    # List of subnet ids to attach to load balancer
    subnets: list[str] | None
    # VPC service will be deployed into. Service and tasks will be deployed into public
    # subnets, from this VPC
    vpc_id: pulumi.Output[str]
    # List of action blocks for listener
    default_actions: list[ListenerDefaultActionArgs] | None = None
    # Type of Deployment Controller used for service and tasks. Only ECS supported
    _deployment_controller: DeploymentControllerTypes = DeploymentControllerTypes.ecs
    # Lastest Fargate version will always be used
    _fargate_platform_version: str = "latest"
    # Launch type for service and tasks. Only FARGATE is supported
    _launch_type: LaunchTypes = LaunchTypes.fargate
    # Defines what type of load balancer to use. Always set to 'application'
    _load_balancer_type: str = "application"
    model_config = ConfigDict(arbitrary_types_allowed=True)


class OLApplicationLoadBalancedFargateService(pulumi.ComponentResource):
    def __init__(
        self,
        config: OLApplicationLoadBalancedFargateConfig,
        opts: pulumi.ResourceOptions | None = None,
    ):
        super().__init__(
            "ol:infrastructure:aws:ecs:OLALBFargatService", config.name, None, opts
        )

        resource_options = pulumi.ResourceOptions(parent=self).merge(opts)

        # All subnets in the found VPC will be used for the LB
        subnets = get_subnet_ids(
            vpc_id=config.vpc_id,
        )

        pulumi.log.debug("Retrieve subnet ids from vpc")

        ingress_cidr = ["0.0.0.0/0"]
        if not config.listener_open_to_all_traffic:
            # TODO: need to provide capabilitiy for caller to set IP range  # noqa: E501, FIX002, TD002
            msg = "If listener is not open to all IP ranges, valid CIDR block must be present"  # noqa: E501
            raise ValueError(msg)

        # Certificate, hosted zone, and domain are all required if HTTPs is to be
        # supported
        if config.load_balancer_protocol == Protocol.https:
            if not config.zone_name:
                msg = "HTTPs protocol must be accompanied by a valid Route 53 Zone"
                raise ValueError(msg)

            if not config.domain_name:
                msg = "HTTPs protocol must be accompanied by a valid domain name"
                raise ValueError(msg)

        # This SG will be used to provide correct type of access to the LB. We will
        # force TCP protocol and only
        lb_security_group = SecurityGroup(
            f"{config.service_name}_lb_sg",
            vpc_id=config.vpc_id,
            ingress=[
                SecurityGroupIngressArgs(
                    protocol="tcp",
                    from_port=config.listener_port,
                    to_port=config.listener_port,
                    cidr_blocks=ingress_cidr,
                )
            ],
            egress=[
                SecurityGroupEgressArgs(
                    protocol="-1", from_port=0, to_port=0, cidr_blocks=["0.0.0.0/0"]
                )
            ],
            tags=config.tags,
            opts=resource_options,
        )

        pulumi.log.debug(
            f"Security group for load balancer created for port {config.listener_port}"
        )

        if config.load_balancer:
            self.load_balancer = config.load_balancer

            pulumi.log.debug("Existing load balancer utilized")
        else:
            # Only Application load balancer types are supported
            self.load_balancer = LoadBalancer(
                f"{config.service_name}-lb",
                access_logs=config.access_log,
                internal=config.internal,
                ip_address_type=config.ip_address_type,
                name=config.load_balancer_name,
                subnets=subnets.ids,
                load_balancer_type=config._load_balancer_type,  # noqa: SLF001
                security_groups=[lb_security_group],
                opts=resource_options,
                tags=config.tags,
            )

            pulumi.log.debug("New load balancer created")

        # target group will be used to attach containers to LB
        target_group = self.build_target_group(config, resource_options)

        # our default action will always be forward to the appropriate target group
        default_actions = [
            ListenerDefaultActionArgs(type="forward", target_group_arn=target_group.arn)
        ]

        # Protocol is either HTTP or HTTPS; ssl_policy is allowed and required only for
        # HTTPS
        listener_protocol, ssl_policy, redirect = self.build_protocol_details(config)
        if redirect:
            pulumi.log.debug("HTTP requests will be redirected to HTTPS")

            default_actions.append(redirect)

        cert_arn = None
        if listener_protocol == Protocol.https:
            route53_record, cert_arn = self.build_domain_resources(
                config, self.load_balancer, resource_options
            )
            self.record = route53_record

        self.listener = Listener(
            f"{config.service_name}-listener",
            load_balancer_arn=self.load_balancer.arn,
            port=config.listener_port,
            certificate_arn=cert_arn,
            protocol=listener_protocol,
            ssl_policy=ssl_policy,
            default_actions=default_actions,
            tags=config.tags,
            opts=resource_options,
        )

        # TODO: route 53, hosted zone, record created  # noqa: FIX002, TD002

        # Only containers that explicitly asked to be attached to TG will
        load_balancer_configuration = self.attach_containers_to_target_group(
            config, target_group
        )

        config = OLFargateServiceConfig(
            service_name=config.service_name,
            desired_count=config.desired_count,
            assign_public_ip=config.assign_public_ip,
            vpc_id=config.vpc_id,
            task_definition_config=config.task_definition_config,
            security_groups=config.security_groups,
            load_balancer_configuration=load_balancer_configuration,
            deployment_circuit_breaker_enabled=config.deployment_circuit_breaker_enabled,
            health_check_grace_period_seconds=config.health_check_grace_period_seconds,
            deployment_max_percent=config.deployment_max_percent,
            deployment_min_percent=config.deployment_min_percent,
            enable_ecs_managed_tags=config.enable_ecs_managed_tags,
            tags=config.tags,
            opts=resource_options,
        )

        self.service = OLFargateService(config)

        component_outputs = {
            "cluster": self.service.cluster,
            "service": self.service.service,
            "task_definition": self.service.task_definition,
            "load_balancer": self.load_balancer,
            "listener": self.listener,
        }

        self.register_outputs(component_outputs)

    def build_protocol_details(
        self, config: OLApplicationLoadBalancedFargateConfig
    ) -> tuple[str, str, ListenerDefaultActionArgs]:
        """Build the protocol related data for an ALB Listener.

        :param config: Configuration object for parameterizing deployment of Fargate
            services deployed behind a load balancer
        :type config: OLApplicationLoadBalancedFargateConfig

        :returns: The information required for setting up the ALB listener definition
                  for an ECS task.

        :rtype: Tuple[str, str, ListenterDefaultActionArgs]
        """
        listener_protocol = "HTTP"
        ssl_policy = ""
        redirect = None

        if config.load_balancer_protocol == Protocol.https:
            listener_protocol = "HTTPS"

            # TODO: allow this to be configured  # noqa: FIX002, TD002
            ssl_policy = "ELBSecurityPolicy-2016-08"
            config.listener_port = 443

            # redirect all http -> https if caller asks
            if config.redirect_http:
                redirect = ListenerDefaultActionArgs(
                    type="redirect",
                    redirect=ListenerDefaultActionRedirectArgs(
                        port="443", protocol="HTTPS", status_code="HTTP_301"
                    ),
                )

        pulumi.log.debug(
            f"{listener_protocol} being utilized for load balancer listener"
        )

        return listener_protocol, ssl_policy, redirect

    def build_target_group(
        self, config: OLApplicationLoadBalancedFargateConfig, opts: ResourceOptions
    ) -> TargetGroup:
        """Construct target group used for attaching containers to the LB.

        :param config: Configuration object for parameterizing deployment of Fargate
            services deployed behind a load balancer
        :type config: OLApplicationLoadBalancedFargateConfig

        :param opts: The resource options to use for customizing created Pulumi
            resources
        :type opts: ResourceOptions

        :returns: A target group definition for connecting the ECS task with the ALB

        :rtype: TargetGroup
        """
        # by default, our target port and protocol will be set to 80/http. To support
        # 443/https from LB to ECS, we need additional SSL certificate(s)
        target_port = DEFAULT_HTTP_PORT
        target_protocol = "HTTP"
        if config.target_protocol == Protocol.https:
            target_port = DEFAULT_HTTPS_PORT
            target_protocol = "HTTPS"

        return TargetGroup(
            f"{config.service_name}-tg",
            port=target_port,  # NOTE: HTTPS requires an additional certificate
            protocol=target_protocol,
            target_type="ip",
            vpc_id=config.vpc_id,
            tags=config.tags,
            opts=opts,
        )

    def attach_containers_to_target_group(
        self, config: OLApplicationLoadBalancedFargateConfig, target_group: TargetGroup
    ) -> list[ServiceLoadBalancerArgs]:
        """Iterate through all container definitions and attach to LB.

        :param config: Configuration object for parameterizing deployment of Fargate
            services deployed behind a load balancer
        :type config: OLApplicationLoadBalancedFargateConfig

        :param target_group: The ALB target group to use for attaching the Fargate
            containers to the configured ALB
        :type target_group: TargetGroup

        :returns: A list of arguments for passing to the ECS service definition for
                  connecting to the load balancer.

        :rtype: List[ServiceLoadBalancerArgs]
        """
        load_balancer_configuration = []
        # need to create lb mappings
        for container in config.task_definition_config.container_definition_configs:
            # if container asks to be attached, we will attach it to the target group
            # created that is mounted to the LB
            if container.attach_to_load_balancer:
                pulumi.log.debug(
                    f"Container {container.container_name} will be attached to load "
                    "balancer"
                )
                load_balancer_configuration.append(
                    ServiceLoadBalancerArgs(
                        target_group_arn=target_group.arn,
                        container_name=container.container_name,
                        container_port=container.container_port,
                    )
                )

        return load_balancer_configuration

    def build_domain_resources(
        self,
        config: OLApplicationLoadBalancedFargateConfig,
        load_balancer: LoadBalancer,
        opts: ResourceOptions,
    ) -> tuple[Record, str]:
        zone = get_zone(config.zone_name)
        cert = get_certificate(config.zone_name, most_recent=True, statuses=["ISSUED"])

        pulumi.log.debug(
            f"Route 53 zone {config.zone_name} and ACM certificate {config.zone_name} "
            "retrieved"
        )

        record = None
        if config.route53_record_type == Route53RecordType.alias:
            record_type = "A"
            record = Record(
                f"{config.name}-r53-record",
                zone_id=zone.id,
                name=config.domain_name,
                type=record_type,
                aliases=[
                    RecordAliasArgs(
                        name=load_balancer.dns_name,
                        zone_id=load_balancer.zone_id,
                        evaluate_target_health=True,
                    )
                ],
                opts=opts,
            )

        elif config.route53_record_type == Route53RecordType.cname:
            msg = "CNAME records are not supported"
            raise ValueError(msg)
        else:
            msg = "Route 53 record type must be ALIAS or CNAME"
            raise ValueError(msg)

        return record, cert.arn
