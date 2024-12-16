from pydantic import Field
from typing_extensions import Annotate

class OLFargateContainerDefinitionConfig(BaseModel):
    container_name: str = Field(
        description="Name of the container in the task config",
        parameter_name="name",
    )
    memory: Optional[PositiveInt] = Field(
        PositiveInt(HALF_GIGABYTE_MB),
        description=(
            "Memory reserved for this container. "
            "If container exceeds this amount, it will be killed"
        ),
        parameter_name="memory",
    )
    image: str = Field(
        description=(
            "Fully qualified (registry/repository:tag) where ECS agent "
            "can retrieve image"
        ),
        parameter_name="image",
    )
    memory_reservation: Optional[PositiveInt] = Field(
        None,
        description="Soft limit of memory to reserve for the container",
        parameter_name="memoryReservation",
    )
    container_port: PositiveInt = Field(
        PositiveInt(DEFAULT_HTTP_PORT),
        description="What port will be assigned to container.",
        parameter_name="containerPort",
    )
    command: Optional[list[str]] = Field(
        None,
        description="The command that is passed to the container",
        parameter_name="command",
    )
    cpu: Optional[PositiveInt] = Field(
        None,
        description="Number of cpu units reserved for container",
        parameter_name="cpu",
    )
    is_essential: bool = Field(
        False,  # noqa: FBT003
        description=(
            "Enabling this flag means if this container stops or fails, "
            "all other containers that are part of the task are stopped"
        ),
        parameter_name="essential",
    )
    environment: Optional[dict[str, str]] = Field(
        None,
        description="Environment variables to pass to container",
        parameter_name="environment",
    )
    secrets: Optional[list[Secret]] = Field(
        None,
        description="Secrets that will be exposed to your container",
        parameter_name="secrets",
    )
    log_configuration: Optional[OLContainerLogConfig] = Field(
        None,
        description="Configuration for setting up log outputs for this container",
        parameter_name="logConfiguration",
    )
    privileged: bool = Field(
        False,  # noqa: FBT003
        description=(
            "If enabled, container is given elevated privileges, similar to 'root' user"
        ),
        parameter_name="privileged",
    )
    attach_to_load_balancer: bool = Field(
        False,  # noqa: FBT003
        description=(
            "If set to True, container will be attached to target group and "
            "load balancer using the port_mappings name and container port"
        ),
        parameter_name="attachToLoadBalancer",
    )
    volumes_from: Optional[list[dict[str, str]]] = Field(
        None,
        description=(
            "Allow for mounting paths between containers. Useful for rendering "
            "configuration templates via Vault agent or consul-template sidecars."
        ),
        parameter_name="volumesFrom",
    )
    model_config = ConfigDict(arbitrary_types_allowed=True)
