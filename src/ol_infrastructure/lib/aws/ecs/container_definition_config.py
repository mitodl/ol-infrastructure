from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, PositiveInt

from bridge.lib.magic_numbers import DEFAULT_HTTP_PORT, HALF_GIGABYTE_MB
from ol_infrastructure.lib.pulumi_helper import StackInfo


def build_container_log_options(
    service_name: str,
    task_name: str,
    stack_info: StackInfo,
    container_name: str | None = None,
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
    # TODO: Put list of options in Enum object and set as type (TMM 2021-09-15)  # noqa: E501, FIX002, TD002
    # Possible values are: "awslogs", "fluentd", "gelf", "json-file", "journald",
    # "logentries", "splunk", "syslog", "awsfirelens"
    log_driver: str
    # Options to pass to log config
    options: dict[str, str] | None = None
    secret_options: list[Secret] | None = None
    model_config = ConfigDict(arbitrary_types_allowed=True)


# Many more options available (in AWS) that are not defined in this configuration
# https://docs.aws.amazon.com/AmazonECS/latest/APIReference/API_ContainerDefinition.html
class OLFargateContainerDefinitionConfig(BaseModel):
    container_name: Annotated[
        str,
        Field(
            description="Name of the container in the task config",
            parameter_name="name",
        ),
    ]
    memory: Annotated[
        PositiveInt | None,
        Field(
            description=(
                "Memory reserved for this container. "
                "If container exceeds this amount, it will be killed"
            ),
            parameter_name="memory",
        ),
    ] = PositiveInt(HALF_GIGABYTE_MB)
    image: Annotated[
        str,
        Field(
            description=(
                "Fully qualified (registry/repository:tag) where ECS agent "
                "can retrieve image"
            ),
            parameter_name="image",
        ),
    ]
    memory_reservation: Annotated[
        PositiveInt | None,
        Field(
            description="Soft limit of memory to reserve for the container",
            parameter_name="memoryReservation",
        ),
    ] = None
    container_port: Annotated[
        PositiveInt,
        Field(
            description="What port will be assigned to container.",
            parameter_name="containerPort",
        ),
    ] = PositiveInt(DEFAULT_HTTP_PORT)
    command: Annotated[
        list[str] | None,
        Field(
            description="The command that is passed to the container",
            parameter_name="command",
        ),
    ] = None
    cpu: Annotated[
        PositiveInt | None,
        Field(
            description="Number of cpu units reserved for container",
            parameter_name="cpu",
        ),
    ] = None
    is_essential: Annotated[
        bool,
        Field(
            description=(
                "Enabling this flag means if this container stops or fails, "
                "all other containers that are part of the task are stopped"
            ),
            parameter_name="essential",
        ),
    ] = False
    environment: Annotated[
        dict[str, str] | None,
        Field(
            description="Environment variables to pass to container",
            parameter_name="environment",
        ),
    ] = None
    secrets: Annotated[
        list[Secret] | None,
        Field(
            description="Secrets that will be exposed to your container",
            parameter_name="secrets",
        ),
    ] = None
    log_configuration: Annotated[
        OLContainerLogConfig | None,
        Field(
            description="Configuration for setting up log outputs for this container",
            parameter_name="logConfiguration",
        ),
    ] = None
    privileged: Annotated[
        bool,
        Field(
            description=(
                "If enabled, container is given elevated privileges, similar to 'root'"
                " user"
            ),
            parameter_name="privileged",
        ),
    ] = False
    attach_to_load_balancer: Annotated[
        bool,
        Field(
            description=(
                "If set to True, container will be attached to target group and "
                "load balancer using the port_mappings name and container port"
            ),
        ),
    ] = False
    volumes_from: Annotated[
        list[dict[str, str]] | None,
        Field(
            description=(
                "Allow for mounting paths betwen containers. Useful for rendering "
                "configuration templates via Vault agent or consul-template sidecars."
            ),
            parameter_name="volumesFrom",
        ),
    ] = None
    model_config = ConfigDict(arbitrary_types_allowed=True)
