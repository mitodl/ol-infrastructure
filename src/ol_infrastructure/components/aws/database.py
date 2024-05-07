"""This module defines a Pulumi component resource for encapsulating our best practices for building RDS instances.

This includes:

- Create a parameter group for the database
- Create and configure a backup policy
- Manage the root user password
- Create relevant security groups
- Create DB instance
"""  # noqa: E501

from enum import Enum
from typing import Optional, Union

import pulumi
from pulumi_aws import rds
from pulumi_aws.ec2 import SecurityGroup
from pydantic import (
    BaseModel,
    ConfigDict,
    PositiveInt,
    SecretStr,
    computed_field,
    conint,
    field_validator,
    model_validator,
)

from ol_infrastructure.components.aws.cloudwatch import (
    OLCloudWatchAlarmSimpleRDS,
    OLCloudWatchAlarmSimpleRDSConfig,
)
from ol_infrastructure.lib.aws.rds_helper import (
    DBInstanceTypes,
    db_engines,
    max_minor_version,
    parameter_group_family,
)
from ol_infrastructure.lib.ol_types import AWSBase

MAX_BACKUP_DAYS = 35


class StorageType(str, Enum):
    """Container for constraining available selection of storage types."""

    magnetic = "standard"
    ssd = "gp3"
    performance = "io1"


class OLReplicaDBConfig(BaseModel):
    """Configuration object for defining configuration needed to create a read replica."""  # noqa: E501

    instance_size: str = DBInstanceTypes.medium
    storage_type: StorageType = StorageType.ssd
    public_access: bool = False
    security_groups: Optional[list[SecurityGroup]] = None
    model_config = ConfigDict(arbitrary_types_allowed=True)


class OLDBConfig(AWSBase):
    """Configuration object for defining the interface to create an RDS instance with sane defaults."""  # noqa: E501

    engine: str
    engine_full_version: Optional[str] = None
    engine_major_version: Optional[str | int] = None
    instance_name: str  # The name of the RDS instance
    password: SecretStr
    parameter_overrides: list[dict[str, Union[str, bool, int, float]]]
    port: PositiveInt
    subnet_group_name: Union[str, pulumi.Output[str]]
    security_groups: list[SecurityGroup]
    backup_days: conint(ge=0, le=MAX_BACKUP_DAYS, strict=True) = 30  # type: ignore  # noqa: PGH003
    db_name: Optional[str] = None  # The name of the database schema to create
    instance_size: str = DBInstanceTypes.general_purpose_large.value
    max_storage: Optional[PositiveInt] = None  # Set to allow for storage autoscaling
    multi_az: bool = True
    prevent_delete: bool = True
    public_access: bool = False
    take_final_snapshot: bool = True
    storage: PositiveInt = PositiveInt(50)
    storage_type: StorageType = StorageType.ssd
    username: str = "oldevops"
    read_replica: Optional[OLReplicaDBConfig] = None
    monitoring_profile_name: str
    model_config = ConfigDict(arbitrary_types_allowed=True)

    @field_validator("engine")
    @classmethod
    def is_valid_engine(cls, engine: str) -> str:
        valid_engines = db_engines()
        if engine not in valid_engines:
            msg = "The specified DB engine is not a valid option in AWS."
            raise ValueError(msg)
        return engine

    @model_validator(mode="after")
    def engine_version_is_set(self):
        if all((self.engine_full_version, self.engine_major_version)):
            msg = (
                "Only one of 'engine_full_version' and 'engine_major-version' can "
                "be set at the same time. Please set one or the other depending on "
                "your preferred behavior."
            )
            raise ValueError(msg)
        if self.engine_full_version is None and self.engine_major_version is None:
            msg = (
                "You must specify either the major version or the full version of the "
                "database engine."
            )
            raise ValueError(msg)
        return self

    # @field_validator("engine_version")
    # @classmethod
    def is_valid_version(
        self, engine_version: str
    ) -> str:  # , info: ValidationInfo) -> str:
        engine = self.engine
        engines_map = db_engines()
        if engine_version not in engines_map.get(engine, []):
            msg = (
                f"The specified version of the {engine} engine is not supported in AWS."
            )
            raise ValueError(msg)
        return engine_version

    @field_validator("monitoring_profile_name")
    @classmethod
    def is_valid_monitoring_profile(cls, monitoring_profile_name: str) -> str:
        valid_monitoring_profile_names = ("production", "qa", "ci", "disabled")
        if monitoring_profile_name not in valid_monitoring_profile_names:
            msg = f"The specified monitoring profile: {monitoring_profile_name} is not valid."  # noqa: E501
            raise ValueError(msg)
        return monitoring_profile_name

    @computed_field  # type: ignore[misc]
    @property
    def engine_version(self) -> str:
        return self.is_valid_version(
            self.engine_full_version
            or max_minor_version(self.engine, self.engine_major_version)
        )


class OLPostgresDBConfig(OLDBConfig):
    """Configuration container to specify settings specific to Postgres."""

    engine: str = "postgres"
    engine_major_version: str | int = "16"
    port: PositiveInt = PositiveInt(5432)
    parameter_overrides: list[dict[str, Union[str, bool, int, float]]] = [  # noqa: RUF012
        {"name": "client_encoding", "value": "UTF-8"},
        {"name": "timezone", "value": "UTC"},
        {"name": "rds.force_ssl", "value": 1},
        {"name": "autovacuum", "value": 1},
    ]


class OLMariaDBConfig(OLDBConfig):
    """Configuration container to specify settings specific to MariaDB."""

    engine: str = "mariadb"
    engine_major_version: str | int = "11"
    port: PositiveInt = PositiveInt(3306)
    parameter_overrides: list[dict[str, Union[str, bool, int, float]]] = [  # noqa: RUF012
        {"name": "character_set_client", "value": "utf8mb4"},
        {"name": "character_set_connection", "value": "utf8mb4"},
        {"name": "character_set_database", "value": "utf8mb4"},
        {"name": "character_set_filesystem", "value": "utf8mb4"},
        {"name": "character_set_results", "value": "utf8mb4"},
        {"name": "character_set_server", "value": "utf8mb4"},
        {"name": "collation_server", "value": "utf8mb4_unicode_ci"},
        {"name": "collation_connection", "value": "utf8mb4_unicode_ci"},
        {"name": "time_zone", "value": "UTC"},
    ]


class OLAmazonDB(pulumi.ComponentResource):
    """Component to create an RDS instance with sane defaults and manage associated resources."""  # noqa: E501

    def __init__(
        self, db_config: OLDBConfig, opts: Optional[pulumi.ResourceOptions] = None
    ):
        """Create an RDS instance, parameter group, and optionally read replica.

        :param db_config: Configuration object for customizing the deployed database
            instance.
        :type db_config: OLDBConfig

        :param opts: Custom pulumi options to pass to child resources.
        :type opts: pulumi.ResourceOptions
        """
        super().__init__(
            "ol:infrastructure:aws:database:OLAmazonDB",
            db_config.instance_name,
            None,
            opts,
        )

        resource_options = pulumi.ResourceOptions(parent=self).merge(opts)

        if db_config.read_replica and db_config.engine == "postgres":
            # Necessary to allow for long-running sync queries from the replica
            # https://docs.airbyte.com/integrations/sources/postgres/#sync-data-from-postgres-hot-standby-server
            # (TMM 2022-11-02)
            db_config.parameter_overrides.append(
                {"name": "hot_standby_feedback", "value": 1}
            )
        self.parameter_group = rds.ParameterGroup(
            f"{db_config.instance_name}-{db_config.engine}-parameter-group",
            family=parameter_group_family(db_config.engine, db_config.engine_version),
            opts=resource_options.merge(
                pulumi.ResourceOptions(ignore_changes=["family"])
            ),
            name=f"{db_config.instance_name}-{db_config.engine}-parameter-group",
            tags=db_config.tags,
            parameters=db_config.parameter_overrides,
        )

        self.db_instance = rds.Instance(
            f"{db_config.instance_name}-{db_config.engine}-instance",
            allocated_storage=db_config.storage,
            allow_major_version_upgrade=True,
            backup_retention_period=db_config.backup_days,
            copy_tags_to_snapshot=True,
            db_name=db_config.db_name,
            db_subnet_group_name=db_config.subnet_group_name,
            deletion_protection=db_config.prevent_delete,
            engine=db_config.engine,
            engine_version=db_config.engine_version,
            final_snapshot_identifier=(
                f"{db_config.instance_name}-{db_config.engine}-final-snapshot"
            ),
            identifier=db_config.instance_name,
            instance_class=db_config.instance_size,
            max_allocated_storage=db_config.max_storage,
            multi_az=db_config.multi_az,
            opts=resource_options,
            parameter_group_name=self.parameter_group.name,
            password=db_config.password.get_secret_value(),
            port=db_config.port,
            publicly_accessible=db_config.public_access,
            skip_final_snapshot=not db_config.take_final_snapshot,
            storage_encrypted=True,
            storage_type=db_config.storage_type.value,
            tags=db_config.tags,
            username=db_config.username,
            vpc_security_group_ids=[group.id for group in db_config.security_groups],
        )

        component_outputs = {
            "parameter_group": self.parameter_group,
            "rds_instance": self.db_instance,
        }

        if db_config.read_replica:
            self.db_replica = rds.Instance(
                f"{db_config.instance_name}-{db_config.engine}-replica",
                identifier=f"{db_config.instance_name}-replica",
                instance_class=db_config.read_replica.instance_size,
                kms_key_id=self.db_instance.kms_key_id,
                max_allocated_storage=db_config.max_storage,
                opts=resource_options,
                publicly_accessible=db_config.read_replica.public_access,
                replicate_source_db=self.db_instance.identifier,
                skip_final_snapshot=True,
                storage_encrypted=True,
                storage_type=db_config.read_replica.storage_type.value,
                tags=db_config.tags,
                vpc_security_group_ids=[
                    group.id
                    for group in db_config.read_replica.security_groups
                    or db_config.security_groups
                ],
            )
            component_outputs["rds_replica"] = self.db_replica

        self.register_outputs(component_outputs)

        if db_config.monitoring_profile_name:
            monitoring_profile = self._get_default_monitoring_profile(
                db_config.monitoring_profile_name
            )
            for alarm_name, alarm_args in monitoring_profile.items():
                alarm_config = OLCloudWatchAlarmSimpleRDSConfig(
                    database_identifier=db_config.instance_name,
                    name=f"{db_config.instance_name}-{alarm_name}-OLCloudWatchAlarmSimpleRDSConfig",
                    **alarm_args,
                )
                OLCloudWatchAlarmSimpleRDS(alarm_config=alarm_config)

    def _get_default_monitoring_profile(self, profile_name: str):
        if profile_name == "disabled":
            return {}
        global_profiles = {
            "CPUUtilization": {
                "comparison_operator": "GreaterThanThreshold",
                "description": "RDS - Extended High CPU Utilization",
                "datapoints_to_alarm": 6,
                "level": "warning",
                "period": 300,  # 5 minutes
                "evaluation_periods": 6,  # 30 minutes
                "metric_name": "CPUUtilization",
                "threshold": 50,  # percent
                "unit": "Percent",
            },
            "FreeStorageSpace": {
                "comparison_operator": "LessThanThreshold",
                "description": "RDS - Low Disk Space Remaining",
                "datapoints_to_alarm": 6,
                "level": "warning",
                "period": 300,  # 5 minutes
                "evaluation_periods": 6,  # 30 minutes
                "metric_name": "FreeStorageSpace",
                "threshold": 5368709120,  # 5 gigabytes
                "unit": "Bytes",
            },
            "WriteLatency": {
                "comparison_operator": "GreaterThanThreshold",
                "description": "RDS - High Write Latency",
                "datapoints_to_alarm": 6,
                "level": "warning",
                "period": 300,  # 5 minutes
                "evaluation_periods": 6,  # 30 minutes
                "metric_name": "WriteLatency",
                "threshold": 0.050,  # 50 milliseconds
            },
            "ReadLatency": {
                "comparison_operator": "GreaterThanThreshold",
                "description": "RDS - High Read Latency",
                "datapoints_to_alarm": 2,
                "level": "warning",
                "period": 300,  # 5 minutes
                "evaluation_periods": 2,  # 10 minutes
                "metric_name": "ReadLatency",
                "threshold": 0.010,  # 10 milliseconds
            },
        }

        monitoring_profiles = {
            "ci": {},
            "qa": {},
            "production": {
                "EBSIOBlance": {
                    "comparison_operator": "LessThanThreshold",
                    "description": "RDS - EBS IO Balance Remaining",
                    "datapoints_to_alarm": 2,
                    "level": "warning",
                    "period": 300,  # 5 minutes
                    "evaluation_periods": 2,  # 10 minutes
                    "metric_name": "EBSIOBalance%",
                    "threshold": 75,  # percent
                    "unit": "Percent",
                },
                "DiskQueueDepth": {
                    "comparison_operator": "GreaterThanThreshold",
                    "description": "RDS - Disk Queue Depth - Requests waiting",
                    "datapoints_to_alarm": 2,
                    "level": "warning",
                    "period": 300,  # 5 minutes
                    "evaluation_periods": 2,  # 10 minutes
                    "metric_name": "DiskQueueDepth",
                    "threshold": 1,  # requests
                },
            },
        }
        return dict(
            **global_profiles,
            **(monitoring_profiles[profile_name]),
        )
