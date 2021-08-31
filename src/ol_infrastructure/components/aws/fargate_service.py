"""This module defines a Pulumi component resource for encapsulating our best practices for building ECS Fargate Services.

This includes:

- ECS Cluster
- ECS Service
- ECS Task Definition - Single image task or multiple images (sidecar)
- EC2 Security Group(s)

Optional:
- EC2 Load Balancer
- EC2 Listener
- EC2 Target Group
- Route53 Zone
- ACM Certificate

Required On Input:
- VPC
- Subnets (Implicit)
"""
import json

import pulumi
from pulumi_aws import ecs
from pulumi_aws.ecs import (
    Service,
    Cluster,
    ServiceDeploymentCircuitBreakerArgs,
    TaskDefinition
)

from pulumi_aws.iam import (
    Role,
    RolePolicyAttachment,
    ManagedPolicy
)

from ol_infrastructure.lib.aws.ecs.fargate_service_config import (
    OLFargateServiceConfig
)

class OLFargateService(pulumi.ComponentResource):

    def __init__(self, fargate_config: OLFargateServiceConfig, opts: pulumi.ResourceOptions = None):

        super().__init__(
            "ol:infrastructure:aws:ecs:OLFargateService",
            fargate_config.service_name,
            None,
            opts,
        )

        self.resource_options = pulumi.ResourceOptions(
            parent=self).merge(opts)  # type: ignore

        if fargate_config.cluster:
            pulumi.log.debug(
                f"using existing ECS Cluster '{fargate_config.cluster.id}' provided in arguments")
            self.cluster = fargate_config.cluster
        else:
            pulumi.log.debug("creating new ECS cluster")
            self.cluster = Cluster(
                f"{fargate_config.service_name}_cluster",
                tags=fargate_config.tags,
                opts=self.resource_options
            )

        """We'll enable rollback, as well as, circuit breaker if caller opts in"""
        circuit_breaker = None
        if fargate_config.deployment_circuit_breaker_enabled:

            pulumi.log.debug("ecs service deployment service breaker enabled")

            circuit_breaker = ServiceDeploymentCircuitBreakerArgs(
                enable=True,
                rollback=True
            )

        task_config = fargate_config.task_definition_config

        container_definition = self.build_container_definition(fargate_config)

        pulumi.log.debug("container definitions constructed")

        task_definition = TaskDefinition(
            f"{fargate_config.service_name}_task_def",
            family=task_config.task_def_name,
            cpu=task_config.cpu,
            execution_role_arn=self.get_execution_role_arn(fargate_config),
            memory=task_config.memory_mib,
            tags=fargate_config.tags,
            task_role_arn=task_config.task_execution_role_arn,
            network_mode="awsvpc",
            requires_compatibilities=["FARGATE"],
            container_definitions=container_definition,
            opts=self.resource_options
        )

        service_role_arn = ""
        if fargate_config.service_role:
            service_role_arn = fargate_config.service_role.arn
        
        health_check_grace_period = None
        if fargate_config.load_balancer_configuration:
            health_check_grace_period = fargate_config.health_check_grace_period_seconds

        self.service = Service(
            f"{fargate_config.service_name}_service",
            name=f"{fargate_config.service_name}_service",
            cluster=self.cluster.id,
            desired_count=fargate_config.desired_count,
            iam_role=service_role_arn,
            deployment_maximum_percent=fargate_config.deployment_max_percent,
            deployment_minimum_healthy_percent=fargate_config.deployment_min_percent,
            deployment_controller=fargate_config.get_deployment_controller(),
            deployment_circuit_breaker=circuit_breaker,
            health_check_grace_period_seconds=health_check_grace_period,
            launch_type=fargate_config._launch_type,
            network_configuration=fargate_config.get_service_network_configuration(),
            load_balancers=fargate_config.load_balancer_configuration,
            task_definition=task_definition.arn,
            tags=fargate_config.tags,
            opts=self.resource_options
        )

        component_outputs = {
            "cluster": self.cluster,
            "service": self.service
        }

        self.register_outputs(component_outputs)

    """Container definitions are strings, so we'll need to create task defition from provided arguments"""

    def build_container_definition(self, config: OLFargateServiceConfig) -> str:
        if not config.task_definition_config or not config.task_definition_config.container_definition_configs:
            raise ValueError("At least one container definition must be defined")

        pulumi.log.debug("Creating container task definitions")

        outputs = []
        for container in config.task_definition_config.container_definition_configs:

            log_config = None
            if container.log_configuration:
                log_config = {
                    "logDriver": container.log_configuration.log_driver,
                    "options": container.log_configuration.options,
                    "secretOptions": container.log_configuration.secret_options
                }

            environment = []
            if container.environment:
                for key in container.environment.keys():
                    environment.append({
                        "name": key,
                        "Value": container.environment[key]
                    })

            outputs.append({
                "name": container.container_name,
                "image": container.image,
                "portMappings": [{
                    "containerPort": container.container_port,
                    "containerName": container.container_name,
                    "protocol": "tcp"
                }],
                "memory": container.memory,
                "command": container.command,
                "cpu": container.cpu,
                "environment": environment,
                "essential": container.is_essential,
                "logConfiguration": log_config
            })

        pulumi.log.debug(f"container definitions: {outputs}")

        return json.dumps(outputs)

    """If caller did not provided an execution role arn, we'll build one with base ECS Managed Policy"""

    def get_execution_role_arn(self, config: OLFargateServiceConfig) -> str:
        if config.task_definition_config.execution_role_arn:
            pulumi.log.debug(
                "using task definition execution role arn provided by caller")

            return config.task_definition_config.execution_role_arn
        else:
            pulumi.log.debug(
                "creating new task definition execution role with AmazonEcsTaskExecutionRolePolicy attached")

            role = Role(
                f"{config.task_definition_config.task_def_name}-role",
                assume_role_policy=json.dumps({
                    "Version": "2012-10-17",
                    "Statement": [
                        {
                            "Sid": "",
                            "Effect": "Allow",
                            "Principal": {
                                "Service": "ecs-tasks.amazonaws.com"
                            },
                            "Action": "sts:AssumeRole"
                        }
                    ]
                }),
                tags=config.tags,
                opts=self.resource_options
            )

            RolePolicyAttachment(
                f"{config.task_definition_config.task_def_name}-policy-attachment",
                role=role.name,
                policy_arn=ManagedPolicy.AMAZON_ECS_TASK_EXECUTION_ROLE_POLICY,
                opts=self.resource_options
            )

            return role.arn
