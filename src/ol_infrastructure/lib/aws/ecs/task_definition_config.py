from pulumi import Output
from pydantic import BaseModel, ConfigDict, PositiveInt

from bridge.lib.magic_numbers import HALF_GIGABYTE_MB, ONE_GIGAHERTZ
from ol_infrastructure.lib.aws.ecs.container_definition_config import (
    OLFargateContainerDefinitionConfig,
)


class OLFargateTaskDefinitionConfig(BaseModel):
    """Maps to 'family' property which is unique name for Task Definition."""

    task_def_name: str
    # ARN of IAM role to use for task execution role. Default will be a role created w/
    # AmazonECSTaskExecutionRolePolicy This role allows ECS Agent and Docker daemon to
    # make calls such as:
    # - sending logs to CloudWatch
    # - retrieving image from private ECR repository
    # - task definition is referencing sensitive data using SecretsManager and/or
    #   Parameter Store
    execution_role_arn: Output[str] | None = None
    # ARN of IAM role used for task execution role. Your code will assume this role to
    # make calls to other AWS services
    task_execution_role_arn: Output[str] | None = None
    # CPU allotment for task definition
    cpu: PositiveInt = PositiveInt(ONE_GIGAHERTZ / 4)
    # Memory allotment for task definition
    memory_mib: PositiveInt = PositiveInt(HALF_GIGABYTE_MB)
    # List of container definitions that will be attached to task
    container_definition_configs: list[OLFargateContainerDefinitionConfig]
    model_config = ConfigDict(arbitrary_types_allowed=True)
