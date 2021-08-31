import pulumi
from pulumi_aws.ec2 import (
    SecurityGroup,
    SecurityGroupIngressArgs,
    SecurityGroupEgressArgs
)

from pulumi_aws.cloudwatch import LogGroup

from pulumi_aws.ecs import Cluster

from ol_infrastructure.components.aws.fargate_service import OLFargateService
from ol_infrastructure.components.aws.alb_fargate_service import ApplicationLoadBalancedFargateService
from ol_infrastructure.lib.aws.alb_fargate_service_config import (
    OLApplicationLoadBalancedFargateConfig, Protocol
)
from ol_infrastructure.lib.aws.ecs.fargate_service_config import OLFargateServiceConfig
from ol_infrastructure.lib.aws.ecs.task_definition_config import OLFargateTaskDefinitionConfig
from ol_infrastructure.lib.aws.ecs.container_definition_config import OLContainerLogConfig, OLFargateContainerDefinitionConfig
from ol_infrastructure.lib.ol_types import AWSBase

aws_config = AWSBase(
    tags={"OU": "data", "Environment": "DEV"},
)

vpc_stack = pulumi.StackReference("phillipedwards/ecs-vpc/dev")
vpc_id = vpc_stack.require_output("vpcId")

desired_task_count = 3
circuit_breaker = False
health_check = 60

# Since all tasks are in a public subnet, give them public IPs to interact with internet
assign_public_ip = True

deployment_max_percent = 200
deployment_min_percent = 100

create_external_cluster = True
cluster = None
if create_external_cluster:
    cluster = Cluster(
        "test-ext-cluster",
        tags=aws_config.tags
    )

security_group = SecurityGroup(
    "ecs-task-sec-group",
    vpc_id=vpc_id,
    ingress=[SecurityGroupIngressArgs(
        protocol="tcp",
        from_port=80,
        to_port=80,
        cidr_blocks=["0.0.0.0/0"]
    )],
    egress=[SecurityGroupEgressArgs(
        protocol="-1",
        from_port=0,
        to_port=0,
        cidr_blocks=["0.0.0.0/0"]
    )],
    tags=aws_config.tags
)

# move log group to fargate service
log_group = LogGroup(
    "ecs-log-group",
    name="ecs/test/log_group",
    retention_in_days=1,
    tags=aws_config.tags
)

task_config = OLFargateTaskDefinitionConfig(
    task_def_name="ecs-task-test",
    cpu=512,
    memory_mib=1024,
    container_definition_configs=[OLFargateContainerDefinitionConfig(
        container_name="nginx",
        image="nginx",
        container_port=80,
        attach_to_load_balancer=True,
        memory=1024,
        cpu=512,
        is_essential=True,
        environment={
            "SOME_VAR": "true"
        },
        log_configuration=OLContainerLogConfig(
            log_driver="awslogs",
            options={
                "awslogs-group": "ecs/test/log_group",
                "awslogs-region": "us-west-2",
                "awslogs-stream-prefix": "ecs"
            }
        )
    )],
    tags=aws_config.tags
)

sgs = [security_group]

app = None
create_lb = False
if not create_lb:
    config = OLFargateServiceConfig(
        service_name="ecs-test",
        cluster=cluster,
        desired_count=desired_task_count,
        assign_public_ip=assign_public_ip,
        vpc_id=vpc_id,
        task_definition_config=task_config,
        security_groups=sgs,
        deployment_circuit_breaker_enabled=circuit_breaker,
        deployment_max_percent=deployment_max_percent,
        deployment_min_percent=deployment_min_percent,
        enable_ecs_managed_tags=True,
        tags=aws_config.tags
    )

    app = OLFargateService(
        config
    )

    pulumi.export("service_name", app.service.name)
    pulumi.export("cluster_arn", app.cluster.arn)
else:

    load_balancer_protocol = Protocol.https
    zone = None
    acm_cert = None
    domain = None
    listener_port = 443 if load_balancer_protocol == Protocol.https else 80

    if load_balancer_protocol == Protocol.https:
        hostname = "ndexable.com"
        zone = hostname
        domain = f"api.{hostname}"

    config = OLApplicationLoadBalancedFargateConfig(
        name="ecs-test",
        service_name="ecs-alb-test",
        access_log=None,
        assign_public_ip=assign_public_ip,
        load_balancer_protocol=load_balancer_protocol,
        desired_count=desired_task_count,
        deployment_circuit_breaker_enabled=circuit_breaker,
        deployment_max_percent=deployment_max_percent,
        deployment_min_percent=deployment_min_percent,
        enable_ecs_managed_tags=True,
        domain_name=domain,
        zone_name=zone,
        health_check_grace_period_seconds=health_check,
        listener_port=listener_port,
        load_balancer_name="nginx-lb",
        internal=False,
        listener_open_to_all_traffic=True,
        public_load_balancer=True,
        security_groups=sgs,
        vpc_id=vpc_id,
        task_definition_config=task_config,
        tags=aws_config.tags
    )

    app = ApplicationLoadBalancedFargateService(
        config=config
    )

    pulumi.export("service_name", app.service.service.name)
    pulumi.export("cluster_arn", app.service.cluster.arn)
    pulumi.export("lb_url", app.load_balancer.dns_name)
    pulumi.export("url", app.record.fqdn)
