from os import name
import pulumi
from typing import Tuple, List
from pulumi_aws.ec2.route import Route

from pulumi_aws.route53 import (
    Record,
    RecordAliasArgs,
    get_zone
)

from pulumi_aws.acm import get_certificate

from pulumi_aws.lb import (
    LoadBalancer,
    TargetGroup,
    Listener,
    ListenerDefaultActionArgs,
    ListenerDefaultActionRedirectArgs,
)

from pulumi_aws.ec2 import (
    get_subnet_ids,
    SecurityGroupEgressArgs,
    SecurityGroupIngressArgs,
    SecurityGroup,
)

from pulumi_aws.ecs import ServiceLoadBalancerArgs
from ol_infrastructure.components.aws.fargate_service import OLFargateService

from ol_infrastructure.lib.aws.alb_fargate_service_config import (
    OLApplicationLoadBalancedFargateConfig,
    Protocol,
    Route53RecordType
)

from ol_infrastructure.lib.aws.ecs.fargate_service_config import OLFargateServiceConfig


class ApplicationLoadBalancedFargateService(pulumi.ComponentResource):
    def __init__(self, config: OLApplicationLoadBalancedFargateConfig, opts: pulumi.ResourceOptions = None):

        super().__init__(
            "ol:infrastructure:aws:ecs:OLALBFargatService",
            config.name,
            None,
            opts
        )

        resource_options = pulumi.ResourceOptions(parent=self).merge(opts)

        """All subnets in the found VPC will be used for the LB"""
        subnets = get_subnet_ids(
            vpc_id=config.vpc_id,
        )

        ingress_cidr = ["0.0.0.0/0"]
        if not config.listener_open_to_all_traffic:
            # TODO: need to provide capabilitiy for caller to set IP range
            raise ValueError(
                "If listener is not open to all IP ranges, valid CIDR block must be present")

        """Certificate, hosted zone, and domain are all required if HTTPs is to be supported"""
        if config.load_balancer_protocol == Protocol.https:
            if not config.zone_name:
                raise ValueError(
                    "HTTPs protocol must be accompanied by a valid Route 53 Zone"
                )

            if not config.domain_name:
                raise ValueError(
                    "HTTPs protocol must be accompanied by a valid domain name"
                )

        """This SG will be used to provide correct type of access to the LB. We will force TCP protocol and only """
        lb_security_group = SecurityGroup(
            f"{config.service_name}_lb_sg",
            vpc_id=config.vpc_id,
            ingress=[SecurityGroupIngressArgs(
                protocol="tcp",
                from_port=config.listener_port,
                to_port=config.listener_port,
                cidr_blocks=ingress_cidr
            )],
            egress=[SecurityGroupEgressArgs(
                protocol="-1",
                from_port=0,
                to_port=0,
                cidr_blocks=["0.0.0.0/0"]
            )],
            tags=config.tags
        )

        """Only Application load balancer types are supported"""
        self.load_balancer = LoadBalancer(
            f"{config.service_name}-lb",
            access_logs=config.access_log,
            internal=config.internal,
            ip_address_type=config.ip_address_type,
            name=config.load_balancer_name,
            subnets=subnets.ids,
            load_balancer_type=config._load_balancer_type,
            security_groups=[lb_security_group],
            opts=resource_options,
            tags=config.tags
        )

        # target group will be used to attach containers to LB
        target_group = self.build_target_group(config, resource_options)

        """Our default action will always be forward to the appropriate target group"""
        default_actions = [ListenerDefaultActionArgs(
            type="forward",
            target_group_arn=target_group.arn
        )]

        """Protocol is either HTTP or HTTPS; ssl_policy is allowed and required only for HTTPS"""
        listener_protocol, ssl_policy, redirect = self.build_protocol_details(config)
        if redirect:
            default_actions.append(redirect)

        cert_arn = None
        if listener_protocol == Protocol.https:
            self.record, cert_arn = self.build_domain_resources(config, self.load_balancer)

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

        # TODO: route 53, hosted zone, record created

        """Only containers that explicitly asked to be attached to TG will"""
        load_balancer_configuration = self.attach_containers_to_target_group(
            config, target_group)

        config = OLFargateServiceConfig(
            service_name="ecs-test",
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

        self.service = OLFargateService(
            config
        )

        component_outputs = {
            "cluster": self.service.cluster,
            "service": self.service.service,
            "load_balancer": self.load_balancer,
            "listener": self.listener,
            "record": self.record
        }

        self.register_outputs(component_outputs)

    """Builds the protocol related data for an ALB Listener"""

    def build_protocol_details(self, config: OLApplicationLoadBalancedFargateConfig) -> Tuple[str, str, ListenerDefaultActionArgs]:
        listener_protocol = "HTTP"
        ssl_policy = ""
        redirect = None

        if config.load_balancer_protocol == Protocol.https:
            listener_protocol = "HTTPS"

            # TODO: allow this to be configured
            ssl_policy = "ELBSecurityPolicy-2016-08"
            config.listener_port = 443

            redirect = None
            # redirect all http -> https if caller asks
            if config.redirect_http:
                redirect = ListenerDefaultActionArgs(
                    type="redirect",
                    redirect=ListenerDefaultActionRedirectArgs(
                        port="443",
                        protocol="HTTPS",
                        status_code="HTTP_301"
                    )
                )

        return listener_protocol, ssl_policy, redirect

    """Construct the target group that will be used for attaching containers to the LB"""

    def build_target_group(self, config: OLApplicationLoadBalancedFargateConfig, opts) -> TargetGroup:
        # by default, our target port and protocol will be set to 80/http. To support 443/https from LB to ECS, we need additional SSL certificate(s)
        target_port = 80
        target_protocol = "HTTP"
        if (config.target_protocol == Protocol.https):
            target_port = 443
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

    """Iterate through all container definitions and attach to LB, if the appropriate flag is set"""

    def attach_containers_to_target_group(self, config: OLApplicationLoadBalancedFargateConfig, target_group: TargetGroup) -> List[ServiceLoadBalancerArgs]:
        load_balancer_configuration = []
        # need to create lb mappings
        for container in config.task_definition_config.container_definition_configs:

            # if container asks to be attached, we will attach it to the target group created that is mounted to the LB
            if container.attach_to_load_balancer:
                load_balancer_configuration.append(ServiceLoadBalancerArgs(
                    target_group_arn=target_group.arn,
                    container_name=container.container_name,
                    container_port=container.container_port
                ))

        return load_balancer_configuration

    def build_domain_resources(self, config: OLApplicationLoadBalancedFargateConfig, load_balancer: LoadBalancer) -> Tuple[Record, str]:

        zone = get_zone(config.zone_name)
        cert = get_certificate(
            config.zone_name,
            most_recent=True,
            statuses=["ISSUED"]
        )

        record = None
        if config.route53_record_type == Route53RecordType.alias:
            type = "A"
            record = Record(
                f"{config.name}-r53-record",
                zone_id=zone.id,
                name=config.domain_name,
                type=type,
                aliases=[RecordAliasArgs(
                    name=load_balancer.dns_name,
                    zone_id=load_balancer.zone_id,
                    evaluate_target_health=True
                )]
            )

        elif config.route53_record_type == Route53RecordType.cname:
            raise ValueError(
                "CNAME records are not supported"
            )
        else:
            raise ValueError(
                "Route 53 record type must be ALIAS or CNAME"
            )

        return record, cert.arn
