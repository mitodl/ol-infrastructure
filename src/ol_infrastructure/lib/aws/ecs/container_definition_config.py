from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, PositiveInt

from bridge.lib.magic_numbers import DEFAULT_HTTP_PORT, HALF_GIGABYTE_MB
from ol_infrastructure.lib.pulumi_helper import StackInfo


def build_container_log_options(
    service_name: str,
    task_name: str,
    stack_info: StackInfo,
    container_name: Optional[str] = None,
) -> dict[str, str]:
    return {
        "awslogs-group": f"ecs/{service_name}/{task_name}/{stack_info.env_suffix}/",
        "awslogs-region": "us-east-1",
        "awslogs-stream-prefix": f"{container_name}",
        "awslogs-create-group": "true",
    }


class Secret(BaseModel):
    name: str = Field(..., description="The name of the secret.")
    value_from: str = Field(
        ...,
        alias="valueFrom",
        description=(
            "The secret to expose to the container. The supported values are either the"
            " full ARN of the AWS Secrets Manager secret or the full ARN of the"
            " parameter in the AWS Systems Manager Parameter Store.  If the AWS Systems"
            " Manager Parameter Store parameter exists in the same Region as the task"
            " you are launching, then you can use either the full ARN or name of the"
            " parameter. If the parameter exists in a different Region, then the full"
            " ARN must be specified. "
        ),
    )


class OLContainerLogConfig(BaseModel):
    # TODO: Put list of options in Enum object and set as type (TMM 2021-09-15)  # noqa: E501, FIX002, TD002, TD003
    # Possible values are: "awslogs", "fluentd", "gelf", "json-file", "journald",
    # "logentries", "splunk", "syslog", "awsfirelens"
    log_driver: str
    # Options to pass to log config
    options: Optional[dict[str, str]] = None
    secret_options: Optional[list[Secret]] = None
    model_config = ConfigDict(arbitrary_types_allowed=True)


# Many more options available (in AWS) that are not defined in this configuration
# https://docs.aws.amazon.com/AmazonECS/latest/APIReference/API_ContainerDefinition.html
class OLFargateContainerDefinitionConfig(BaseModel):
    container_name: str = Field(
        ...,
        description="Name of the container in the task config",
        parameter_name="name",  # type: ignore[call-arg]
    )
    memory: Optional[PositiveInt] = Field(
        PositiveInt(HALF_GIGABYTE_MB),
        description=(
            "Memory reserved for this container. "
            "If container exceeds this amount, it will be killed"
        ),
        parameter_name="memory",  # type: ignore[call-arg]
    )
    image: str = Field(
        ...,
        description=(
            "Fully qualified (registry/repository:tag) where ECS agent "
            "can retrieve image"
        ),
        parameter_name="image",  # type: ignore[call-arg]
    )
    memory_reservation: Optional[PositiveInt] = Field(
        None,
        description="Soft limit of memory to reserve for the container",
        parameter_name="memoryReservation",  # type: ignore[call-arg]
    )
    container_port: PositiveInt = Field(
        PositiveInt(DEFAULT_HTTP_PORT),
        description="What port will be assigned to container.",
        parameter_name="containerPort",  # type: ignore[call-arg]
    )
    command: Optional[list[str]] = Field(
        None,
        description="The command that is passed to the container",
        parameter_name="command",  # type: ignore[call-arg]
    )
    cpu: Optional[PositiveInt] = Field(
        None,
        description="Number of cpu units reserved for container",
        parameter_name="cpu",  # type: ignore[call-arg]
    )
    is_essential: bool = Field(
        False,  # noqa: FBT003
        description=(
            "Enabling this flag means if this container stops or fails, "
            "all other containers that are part of the task are stopped"
        ),
        parameter_name="essential",  # type: ignore[call-arg]
    )
    environment: Optional[dict[str, str]] = Field(
        None,
        description="Environment variables to pass to container",
        parameter_name="environment",  # type: ignore[call-arg]
    )
    secrets: Optional[list[Secret]] = Field(
        None,
        description="Secrets that will be exposed to your container",
        parameter_name="secrets",  # type: ignore[call-arg]
    )
    log_configuration: Optional[OLContainerLogConfig] = Field(
        None,
        description="Configuration for setting up log outputs for this container",
        parameter_name="logConfiguration",  # type: ignore[call-arg]
    )
    privileged: bool = Field(
        False,  # noqa: FBT003
        description=(
            "If enabled, container is given elevated privileges, similar to 'root' user"
        ),
        parameter_name="privileged",  # type: ignore[call-arg]
    )
    attach_to_load_balancer: bool = Field(
        False,  # noqa: FBT003
        description=(
            "If set to True, container will be attached to target group and "
            "load balancer using the port_mappings name and container port"
        ),
    )
    volumes_from: Optional[list[dict[str, str]]] = Field(
        None,
        description=(
            "Allow for mounting paths betwen containers. Useful for rendering "
            "configuration templates via Vault agent or consul-template sidecars."
        ),
        parameter_name="volumesFrom",  # type: ignore[call-arg]
    )
    model_config = ConfigDict(arbitrary_types_allowed=True)
