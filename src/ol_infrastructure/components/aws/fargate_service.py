"""This module defines a Pulumi component resource for building ECS Fargate Services.

This module can accept a load balancer, if the caller chooses, but it is not required.

Included:
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
from pulumi_aws.ecs import (
    Cluster,
    Service,
    ServiceDeploymentCircuitBreakerArgs,
    TaskDefinition,
)
from pulumi_aws.iam import ManagedPolicy, Role, RolePolicyAttachment

from ol_infrastructure.lib.aws.ecs.fargate_service_config import OLFargateServiceConfig


class OLFargateService(pulumi.ComponentResource):
    def __init__(
        self, config: OLFargateServiceConfig, opts: pulumi.ResourceOptions = None
    ):

        super().__init__(
            "ol:infrastructure:aws:ecs:OLFargateService",
            config.service_name,
            None,
            opts,
        )

        self.resource_options = pulumi.ResourceOptions(parent=self).merge(
            opts
        )  # type: ignore

        if config.cluster:
            pulumi.log.debug(
                "using existing ECS Cluster '{}' provided in arguments".format(
                    config.cluster.id
                )
            )
            self.cluster = config.cluster
        else:
            pulumi.log.debug("creating new ECS cluster")
            self.cluster = Cluster(
                f"{config.service_name}_cluster",
                tags=config.tags,
                opts=self.resource_options,
            )

        # We'll enable rollback, as well as, circuit breaker if caller opts in
        circuit_breaker = None
        if config.deployment_circuit_breaker_enabled:

            pulumi.log.debug("ecs service deployment service breaker enabled")

            circuit_breaker = ServiceDeploymentCircuitBreakerArgs(
                enable=True, rollback=True
            )

        task_config = config.task_definition_config

        container_definition = self.build_container_definition(config)

        pulumi.log.debug("container definitions constructed")

        self.task_definition = TaskDefinition(
            f"{config.service_name}_task_def",
            family=task_config.task_def_name,
            cpu=task_config.cpu,
            execution_role_arn=self.get_execution_role_arn(config),
            memory=task_config.memory_mib,
            tags=config.tags,
            task_role_arn=task_config.task_execution_role_arn,
            network_mode="awsvpc",
            requires_compatibilities=["FARGATE"],
            container_definitions=container_definition,
            opts=self.resource_options,
        )

        service_role_arn = ""
        if config.service_role:
            pulumi.log.debug(f"Attaching existing service role {config.service_role}")
            service_role_arn = config.service_role.arn

        health_check_grace_period = None
        if config.load_balancer_configuration:
            pulumi.log.debug(
                "Setting health check grace period to "
                f"{config.health_check_grace_period_seconds} seconds"
            )
            health_check_grace_period = config.health_check_grace_period_seconds

        self.service = Service(
            f"{config.service_name}_service",
            name=f"{config.service_name}_service",
            cluster=self.cluster.id,
            desired_count=config.desired_count,
            iam_role=service_role_arn,
            deployment_maximum_percent=config.deployment_max_percent,
            deployment_minimum_healthy_percent=config.deployment_min_percent,
            deployment_controller=config.get_deployment_controller(),
            deployment_circuit_breaker=circuit_breaker,
            health_check_grace_period_seconds=health_check_grace_period,
            launch_type=config._launch_type,  # noqa: WPS437
            network_configuration=config.get_service_network_configuration(),
            load_balancers=config.load_balancer_configuration,
            task_definition=self.task_definition.arn,
            platform_version=config._fargate_platform_version,  # noqa: WPS437
            force_new_deployment=config.force_new_deployment,
            enable_ecs_managed_tags=config.enable_ecs_managed_tags,
            tags=config.tags,
            opts=self.resource_options,
        )

        component_outputs = {
            "cluster": self.cluster,
            "service": self.service,
            "task_definition": self.task_definition,
        }

        self.register_outputs(component_outputs)

    def build_container_definition(self, config: OLFargateServiceConfig) -> str:
        """Create task defition string from provided arguments.

        :param config: Configuration object for parameterizing deployment of Fargate
            services
        :type config: OLFargateServiceConfig

        :raises ValueError: If not configured with a task or container definition
            configuration

        :returns: An AWS ECS container definition object encoded as a string for
                  including in a task definition.

        :rtype: str
        """
        if (  # noqa: WPS337
            not config.task_definition_config
            or not config.task_definition_config.container_definition_configs
        ):
            raise ValueError("At least one container definition must be defined")

        pulumi.log.debug("Creating container task definitions")

        outputs = []
        for container in config.task_definition_config.container_definition_configs:
            log_config = None
            if container.log_configuration:
                log_config = {
                    "logDriver": container.log_configuration.log_driver,
                    "options": container.log_configuration.options,
                    "secretOptions": container.log_configuration.secret_options,
                }

            environment = []
            if container.environment:
                for key in container.environment.keys():
                    environment.append(
                        {"name": key, "value": container.environment[key]}
                    )

            outputs.append(
                {
                    "name": container.container_name,
                    "image": container.image,
                    "portMappings": [
                        {
                            "containerPort": container.container_port,
                            "containerName": container.container_name,
                            "protocol": "tcp",
                        }
                    ],
                    "memory": container.memory,
                    "command": container.command,
                    "cpu": container.cpu,
                    "environment": environment,
                    "essential": container.is_essential,
                    "logConfiguration": log_config,
                }
            )

        pulumi.log.debug(f"container definitions: {outputs}")

        return json.dumps(outputs)

    def get_execution_role_arn(self, config: OLFargateServiceConfig) -> str:
        """Build an an execution role arn with base ECS Managed Policy.

        :param config: Configuration object for parameterizing deployment of Fargate
            services
        :type config: OLFargateServiceConfig

        :returns: The ARN of an execution role to be used by the ECS service.

        :rtype: str
        """
        if config.task_definition_config.execution_role_arn:
            pulumi.log.debug(
                "using task definition execution role arn provided by caller"
            )
            return config.task_definition_config.execution_role_arn

        pulumi.log.debug(
            "creating new task definition execution role with "
            "AmazonEcsTaskExecutionRolePolicy attached"
        )

        role = Role(
            f"{config.task_definition_config.task_def_name}-role",  # noqa: WPS237
            assume_role_policy=json.dumps(
                {
                    "Version": "2012-10-17",
                    "Statement": [
                        {
                            "Sid": "",
                            "Effect": "Allow",
                            "Principal": {"Service": "ecs-tasks.amazonaws.com"},
                            "Action": "sts:AssumeRole",
                        }
                    ],
                }
            ),
            tags=config.tags,
            opts=self.resource_options,
        )

        RolePolicyAttachment(
            f"{config.task_definition_config.task_def_name}-policy-attachment",  # noqa: WPS237, E501
            role=role.name,
            policy_arn=ManagedPolicy.AMAZON_ECS_TASK_EXECUTION_ROLE_POLICY,
            opts=self.resource_options,
        )

        return role.arn
