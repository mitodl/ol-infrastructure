import json

import pulumi
from pulumi_aws.ec2 import (
    SecurityGroup,
    SecurityGroupEgressArgs,
    SecurityGroupIngressArgs,
)

from ol_infrastructure.components.aws.fargate_service import (
    OLFargateService,
    OLFargateServiceConfig,
)
from ol_infrastructure.lib.aws.ecs.container_definition_config import (
    OLContainerLogConfig,
    OLFargateContainerDefinitionConfig,
)
from ol_infrastructure.lib.aws.ecs.task_definition_config import (
    OLFargateTaskDefinitionConfig,
)
from ol_infrastructure.lib.ol_types import AWSBase


class PulumiMocks(pulumi.runtime.Mocks):
    def new_resource(self, args: pulumi.runtime.MockResourceArgs):
        print("TYPE: ", args.typ)  # noqa: T201

        outputs = args.inputs
        if args.typ == "aws:ecs/cluster:Cluster":
            outputs = {
                **args.inputs,
            }
        elif args.typ == "aws:iam/role:Role":
            outputs = {**args.inputs, "arn": exec_role_arn}

        return [f"{args.name}_id", outputs]

    def call(self, args: pulumi.runtime.MockCallArgs):
        output = {}

        for key in args.args:
            print(f"Key - {key}, Value - {args.args[key]}")  # noqa: T201

        if args.token == "aws:ec2/getSubnetIds:getSubnetIds":  # noqa: S105
            vpc_id = args.args["vpcId"]
            output = {"id": vpc_id, "ids": subnet_ids, "vpc_id": vpc_id}

        return output


pulumi.runtime.set_mocks(PulumiMocks())

exec_role_arn = "arn:aws:iam::542799376554:role/aws-service-role/ecs.amazonaws.com/AWSServiceRoleForECS"  # noqa: E501

aws_config = AWSBase(
    tags={"OU": "data", "Environment": "DEV"},
)

base_name = "test-run"
vpc_id = "vpc-03eb7d7b50a80dafc"
subnet_ids = [
    "subnet-0da77f2c073f5d7dd",
    "subnet-0c839a69e91c42739",
    "subnet-083aa2f93a0efa159",
]

security_group = SecurityGroup(
    "ecs-task-sec-group",
    vpc_id=vpc_id,
    ingress=[
        SecurityGroupIngressArgs(
            protocol="tcp", from_port=80, to_port=80, cidr_blocks=["0.0.0.0/0"]
        )
    ],
    egress=[
        SecurityGroupEgressArgs(
            protocol="-1", from_port=0, to_port=0, cidr_blocks=["0.0.0.0/0"]
        )
    ],
    tags=aws_config.tags,
)


class TestClassBaseFargateArguments:
    fargate_service = OLFargateService(
        config=OLFargateServiceConfig(
            vpc_id=pulumi.Output.from_input(vpc_id),
            security_groups=[security_group],
            service_name=base_name,
            task_definition_config=OLFargateTaskDefinitionConfig(
                task_def_name="task-test",
                container_definition_configs=[
                    OLFargateContainerDefinitionConfig(
                        container_name="nginx",
                        image="nginx",
                    )
                ],
                tags=aws_config.tags,
            ),
            tags=aws_config.tags,
        ),
    )

    service = fargate_service.service
    task_def = fargate_service.task_definition
    cluster = fargate_service.cluster

    @pulumi.runtime.test
    def test_base_service_info(self):
        def check_name_tags_ids(args):
            tags, name, cluster_id = args

            assert name == f"{base_name}_service", "Service name mismatch"
            assert "Environment" in tags, "Tags must container environment"
            assert cluster_id == f"{base_name}_cluster_id", "Cluster ID mismatch"

        return pulumi.Output.all(
            self.service.tags, self.service.name, self.cluster.id
        ).apply(check_name_tags_ids)

    @pulumi.runtime.test
    def test_default_deployment_info(self):
        def check_task_info(args):
            (
                deployment_controller,
                deployment_maximum_percent,
                deployment_minimum_healthy_percent,
                desired_count,
            ) = args

            assert deployment_controller["type"] == "ECS", (
                "Deployment controller must be ECS"
            )
            assert deployment_maximum_percent == 100
            assert deployment_minimum_healthy_percent == 50
            assert desired_count == 1

        return pulumi.Output.all(
            self.service.deployment_controller,
            self.service.deployment_maximum_percent,
            self.service.deployment_minimum_healthy_percent,
            self.service.desired_count,
        ).apply(check_task_info)

    @pulumi.runtime.test
    def test_circuit_breaker(self):
        def check_circuit_breaker_is_disabled(args):
            deployment_circuit_breaker = args

            assert deployment_circuit_breaker is None

        return self.service.deployment_circuit_breaker.apply(
            check_circuit_breaker_is_disabled
        )

    @pulumi.runtime.test
    def test_load_balancer_info_is_empty(self):
        def check_load_balancer(args):
            health_check_grace_period_seconds, load_balancers = args

            assert health_check_grace_period_seconds is None
            assert load_balancers is None

        return pulumi.Output.all(
            self.service.health_check_grace_period_seconds, self.service.load_balancers
        ).apply(check_load_balancer)

    @pulumi.runtime.test
    def test_launch_type_platform_version(self):
        def check_launch_type_version(args):
            launch_type, platform_version = args

            assert launch_type == "FARGATE", "Only FARGATE launch type is supported"
            assert platform_version == "LATEST"

        return pulumi.Output.all(
            self.service.launch_type, self.service.platform_version
        ).apply(check_launch_type_version)

    @pulumi.runtime.test
    def test_network_configuration(self):
        def check_network_configuration(args):
            network_configuration = args
            subnets = network_configuration["subnets"]

            for subnet in subnets:
                assert subnet in subnet_ids

            assert network_configuration["assign_public_ip"]

            security_groups = network_configuration["security_groups"]

            for group in security_groups:
                assert group == "ecs-task-sec-group_id"

        return self.service.network_configuration.apply(check_network_configuration)

    @pulumi.runtime.test
    def test_minimum_task_info(self):
        def check_task_info(args):
            family = args

            assert family == "task-test"

        return self.task_def.family.apply(check_task_info)

    @pulumi.runtime.test
    def test_cpu_mem(self):
        def check_cpu_mem(args):
            cpu, memory = args

            assert cpu == 256
            assert memory == 512

        return pulumi.Output.all(self.task_def.cpu, self.task_def.memory).apply(
            check_cpu_mem
        )

    @pulumi.runtime.test
    def test_exeuction_role(self):
        def check_execution_role(args):
            execution_role_arn = args

            assert execution_role_arn == exec_role_arn

        return self.task_def.execution_role_arn.apply(check_execution_role)

    @pulumi.runtime.test
    def test_network_mode(self):
        def check_network_mode(args):
            network_mode = args

            assert network_mode == "awsvpc"

        return self.task_def.network_mode.apply(check_network_mode)

    @pulumi.runtime.test
    def test_capabilities(self):
        def check_capabilities(args):
            requires_compatabilities = args

            assert len(requires_compatabilities) == 1
            assert requires_compatabilities[0] == "FARGATE"

        return self.task_def.requires_compatibilities.apply(check_capabilities)

    @pulumi.runtime.test
    def test_role_arn_is_empty(self):
        def check_role_arn(args):
            task_role_arn = args

            assert task_role_arn is None

        return self.task_def.task_role_arn.apply(check_role_arn)

    @pulumi.runtime.test
    def test_container_def_base(self):
        def check_container_info(args):
            conatiner_definitions = args
            container = json.loads(conatiner_definitions)[0]

            assert container["name"] == "nginx"
            assert container["image"] == "nginx"
            assert container["portMappings"][0]["containerPort"] == 80
            assert container["portMappings"][0]["containerName"] == "nginx"
            assert container["portMappings"][0]["protocol"] == "tcp"

            assert container["memory"] == 512
            assert container["command"] is None
            assert container["cpu"] is None
            assert container["environment"] == []
            assert not container["essential"]
            assert container["logConfiguration"] is None

        return self.task_def.container_definitions.apply(check_container_info)


class TestClassAllFargateArguments:
    assign_public_ip = False
    desired_count = 3
    max_percent = 200
    min_percent = 100

    fargate_service = OLFargateService(
        config=OLFargateServiceConfig(
            vpc_id=pulumi.Output.from_input(vpc_id),
            security_groups=[security_group],
            service_name=base_name,
            assign_public_ip=assign_public_ip,
            desired_count=desired_count,
            deployment_max_percent=max_percent,
            deployment_min_percent=min_percent,
            enable_ecs_managed_tags=True,
            deployment_circuit_breaker_enabled=True,
            force_new_deployment=True,
            task_definition_config=OLFargateTaskDefinitionConfig(
                task_def_name="task-test",
                cpu=512,
                memory_mib=1024,
                container_definition_configs=[
                    OLFargateContainerDefinitionConfig(
                        container_name="nginx",
                        image="nginx",
                        memory=512,
                        cpu=256,
                        container_port=80,
                        is_essential=True,
                        environment={"var": "nginx"},
                        log_configuration=OLContainerLogConfig(
                            log_driver="awslogs", options={"awslogs-group": "ecs-nginx"}
                        ),
                        attach_to_load_balancer=False,
                    ),
                    OLFargateContainerDefinitionConfig(
                        container_name="otel",
                        image="otel",
                        container_port=4317,
                        memory=512,
                        cpu=256,
                        is_essential=False,
                        environment={"var": "otel"},
                        log_configuration=OLContainerLogConfig(
                            log_driver="awslogs", options={"awslogs-group": "ecs-otel"}
                        ),
                        attach_to_load_balancer=False,
                    ),
                ],
                tags=aws_config.tags,
            ),
            tags=aws_config.tags,
        ),
    )

    service = fargate_service.service
    task_def = fargate_service.task_definition
    cluster = fargate_service.cluster

    @pulumi.runtime.test
    def test_explicit_deployment_info(self):
        def check_task_info(args):
            (
                deployment_controller,
                deployment_maximum_percent,
                deployment_minimum_healthy_percent,
                desired_count,
            ) = args

            assert deployment_controller["type"] == "ECS"
            assert deployment_maximum_percent == self.max_percent
            assert deployment_minimum_healthy_percent == self.min_percent
            assert desired_count == self.desired_count

        return pulumi.Output.all(
            self.service.deployment_controller,
            self.service.deployment_maximum_percent,
            self.service.deployment_minimum_healthy_percent,
            self.service.desired_count,
        ).apply(check_task_info)

    @pulumi.runtime.test
    def test_network_configuration(self):
        def check_network_configuration(args):
            network_configuration = args
            subnets = network_configuration["subnets"]

            for key in subnets:
                assert key in subnet_ids

            assert not network_configuration["assign_public_ip"]

            security_groups = network_configuration["security_groups"]

            for key in security_groups:
                assert key == "ecs-task-sec-group_id"

        return self.service.network_configuration.apply(check_network_configuration)

    @pulumi.runtime.test
    def test_circuit_breaker(self):
        def check_circuit_breaker_is_enabled(args):
            deployment_circuit_breaker = args

            assert deployment_circuit_breaker != None  # noqa: E711
            assert deployment_circuit_breaker["enable"]
            assert deployment_circuit_breaker["rollback"]

        return self.service.deployment_circuit_breaker.apply(
            check_circuit_breaker_is_enabled
        )

    @pulumi.runtime.test
    def test_ecs_tags_enabled(self):
        def check_ecs_tags(args):
            enable_ecs_managed_tags = args

            assert enable_ecs_managed_tags

        return self.service.enable_ecs_managed_tags.apply(check_ecs_tags)

    @pulumi.runtime.test
    def test_force_deployment(self):
        def check_force_deployment(args):
            force_new_deployment = args

            assert force_new_deployment

        return self.service.force_new_deployment.apply(check_force_deployment)

    @pulumi.runtime.test
    def test_cpu_memory_for_task_containers(self):
        def check_cpu_mem(args):
            cpu, memory, container_definitions = args
            containers = json.loads(container_definitions)

            assert cpu == 512
            assert memory == 1024

            total_cpu = 0
            total_mem = 0

            for container in containers:
                port_mapping = container["portMappings"][0]
                image = container["image"]
                assert image in {"nginx", "otel"}

                name = container["name"]
                assert name in {"nginx", "otel"}

                assert container["logConfiguration"]["logDriver"] == "awslogs"

                assert container["cpu"] == 256
                assert container["memory"] == 512

                total_cpu += container["cpu"]
                total_mem += container["memory"]

                assert container["command"] == None  # noqa: E711
                if image == "nginx":
                    assert (
                        container["logConfiguration"]["options"]["awslogs-group"]
                        == "ecs-nginx"
                    )
                    assert container["essential"]
                    assert container["environment"][0]["name"] == "var"
                    assert container["environment"][0]["value"] == "nginx"
                    assert port_mapping["containerPort"] == 80
                    assert port_mapping["containerName"] == "nginx"
                    assert port_mapping["protocol"] == "tcp"
                elif image == "otel":
                    assert (
                        container["logConfiguration"]["options"]["awslogs-group"]
                        == "ecs-otel"
                    )
                    assert not container["essential"]
                    assert container["environment"][0]["name"] == "var"
                    assert container["environment"][0]["value"] == "otel"
                    assert port_mapping["containerPort"] == 4317
                    assert port_mapping["containerName"] == "otel"
                    assert port_mapping["protocol"] == "tcp"

            assert int(cpu) == total_cpu
            assert int(memory) == total_mem

        return pulumi.Output.all(
            self.task_def.cpu, self.task_def.memory, self.task_def.container_definitions
        ).apply(check_cpu_mem)
