from typing import Optional

from pulumi_aws.secretsmanager import Secret
from pydantic import BaseModel, PositiveInt

from bridge.lib.magic_numbers import DEFAULT_HTTP_PORT, HALF_GIGABYTE_MB


class OLContainerLogConfig(BaseModel):
    # TODO: Put list of options in Enum object and set as type (TMM 2021-09-15)
    # Possible values are: "awslogs", "fluentd", "gelf", "json-file", "journald",
    # "logentries", "splunk", "syslog", "awsfirelens"
    log_driver: str
    # Options to pass to log config
    options: Optional[dict[str, str]] = None
    secret_options: Optional[list[Secret]] = None

    class Config:
        arbitrary_types_allowed = True


# Many more options available (in AWS) that are not defined in this configuration
class OLFargateContainerDefinitionConfig(BaseModel):
    container_name: str
    # Memory reserved for this container. If container exceeds this amount, it will be
    # killed.
    memory: Optional[PositiveInt] = PositiveInt(HALF_GIGABYTE_MB)
    # Fully qualified (registry/repository:tag) where ECS agent can retrieve image
    image: str
    # Soft limit of memory to reserve for the container
    memory_reservation: Optional[PositiveInt]
    # What port will be assigned to container.
    container_port: PositiveInt = PositiveInt(DEFAULT_HTTP_PORT)
    # The command that is passed to the container
    command: Optional[list[str]] = None
    # Number of cpu units reserved for container
    cpu: Optional[PositiveInt]
    # Enabling this flag means if this container stops or fails, all other containers
    # that are part of the task are stopped
    is_essential: bool = False
    # Environment variables to pass to container
    environment: Optional[dict[str, str]] = None
    # Secrets that will be exposed to your container
    secrets: Optional[list[Secret]] = None
    log_configuration: Optional[OLContainerLogConfig] = None
    # If enabled, container is given elevated privileges, similar to 'root' user
    privileged: bool = False
    # If set to True, container will be attached to target group and load balancer using
    # the port_mappings name and container port
    attach_to_load_balancer: bool = False

    class Config:
        arbitrary_types_allowed = True
