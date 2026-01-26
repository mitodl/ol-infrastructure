"""This module defines a Pulumi component resource for encapsulating our best practices for building RDS instances.

This includes:

- Create a parameter group for the database
- Create and configure a backup policy
- Manage the root user password
- Create relevant security groups
- Create DB instance
"""  # noqa: E501

from enum import Enum

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
    engine_major_version,
    get_parameter_group_parameters,
    get_rds_instance,
    max_minor_version,
    parameter_group_family,
    turn_off_deletion_protection,
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
    security_groups: list[SecurityGroup] | None = None
    model_config = ConfigDict(arbitrary_types_allowed=True)


class OLDBConfig(AWSBase):
    """Configuration object for defining the interface to create an RDS instance with sane defaults."""  # noqa: E501

    engine: str
    engine_full_version: str | None = None
    engine_major_version: str | int | None = None
    backup_days: conint(ge=0, le=MAX_BACKUP_DAYS, strict=True) = 30  # type: ignore  # noqa: PGH003
    db_name: str | None = None  # The name of the database schema to create
    instance_name: str  # The name of the RDS instance
    instance_size: str = DBInstanceTypes.general_purpose_large.value
    # Set to allow for storage autoscaling. Default to 1 TB
    max_storage: PositiveInt | None = 1000
    monitoring_profile_name: str
    multi_az: bool = True
    parameter_overrides: list[dict[str, str | bool | int | float]]
    password: SecretStr
    port: PositiveInt
    prevent_delete: bool = True
    public_access: bool = False
    read_replica: OLReplicaDBConfig | None = None
    security_groups: list[SecurityGroup]
    storage: PositiveInt = PositiveInt(50)
    storage_type: StorageType = StorageType.ssd
    subnet_group_name: str | pulumi.Output[str]
    take_final_snapshot: bool = True
    use_blue_green: bool = False
    blue_green_timeout_minutes: PositiveInt = PositiveInt(60 * 12)
    username: str = "oldevops"  # The name of the admin user for the instance
    enable_iam_auth: bool = True
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

    @computed_field  # type: ignore[prop-decorator]
    @property
    def engine_version(self) -> str:
        return self.is_valid_version(
            self.engine_full_version
            or max_minor_version(self.engine, self.engine_major_version)
        )


class OLPostgresDBConfig(OLDBConfig):
    """Configuration container to specify settings specific to Postgres."""

    engine: str = "postgres"
    engine_major_version: str | int = "18"
    port: PositiveInt = PositiveInt(5432)
    parameter_overrides: list[dict[str, str | bool | int | float]] = [  # noqa: RUF012
        {"name": "autovacuum", "value": 1},
        {"name": "client_encoding", "value": "UTF-8"},
        {"name": "rds.force_ssl", "value": 1},
        {"name": "rds.logical_replication", "value": 1},
        {"name": "timezone", "value": "UTC"},
        {"name": "rds.blue_green_replication_type", "value": "logical"},
    ]


class OLMariaDBConfig(OLDBConfig):
    """Configuration container to specify settings specific to MariaDB."""

    engine: str = "mariadb"
    engine_major_version: str | int = "11.8"
    port: PositiveInt = PositiveInt(3306)
    parameter_overrides: list[dict[str, str | bool | int | float]] = [  # noqa: RUF012
        {"name": "character_set_client", "value": "utf8mb4"},
        {"name": "character_set_connection", "value": "utf8mb4"},
        {"name": "character_set_database", "value": "utf8mb4"},
        {"name": "character_set_filesystem", "value": "utf8mb4"},
        {"name": "character_set_results", "value": "utf8mb4"},
        {"name": "character_set_server", "value": "utf8mb4"},
        {"name": "collation_connection", "value": "utf8mb4_unicode_ci"},
        {"name": "collation_server", "value": "utf8mb4_unicode_ci"},
        {"name": "time_zone", "value": "UTC"},
    ]


class OLAmazonDB(pulumi.ComponentResource):
    """Component to create an RDS instance with sane defaults and manage associated resources."""  # noqa: E501

    def __init__(
        self, db_config: OLDBConfig, opts: pulumi.ResourceOptions | None = None
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

        replica_options = resource_options = (opts or pulumi.ResourceOptions()).merge(
            pulumi.ResourceOptions(parent=self)
        )

        # In order to update the minor versions of an RDS instance with a read replica,
        # the replica has to be upgraded prior to the primary. This block of logic
        # allows us to introspect the state of the primary/replica version and apply the
        # changes to the replica first. This necessitates the program being applied
        # twice in order to update the patch release on the primary.
        primary_engine_version = replica_engine_version = db_config.engine_version
        primary_parameter_group_family = replica_parameter_group_family = (
            parameter_group_family(db_config.engine, db_config.engine_version)
        )
        current_db_state = get_rds_instance(db_config.instance_name)
        deletion_protection_for_primary = db_config.prevent_delete

        # There are a handful of cases that will trigger a blue/green update when that
        # is enabled. Those include:
        # - DB Engine Version: Upgrading or downgrading the major or minor version of
        #   your database engine (e.g., changing from PostgreSQL 14 to 15).
        # - DB Instance Class: Modifying the compute and memory capacity of your
        #   instance.
        # - Storage Configuration: Changing the allocated storage size or storage type
        #   (e.g., from gp2 to gp3).
        # - DB Parameter Group: Applying changes to database parameters that require a
        #   restart or are not dynamically applied.
        # - Option Group: Modifying options associated with your database instance.
        # - Multi-AZ Configuration: Enabling or disabling Multi-AZ for your instance.

        # If the current state of the database differs from the configured values for
        # those attributes it will result in starting a blue/green update. In those
        # cases the deletion protection should be disabled, as this will prevent the
        # blue/green deployment from being cleaned up by Pulumi when it is complete. The
        # ResourceOptions will also need to be updated to increase the timeouts to allow
        # for successful completion of the blue/green update process. Currently it is
        # timing out at 1 hour, so we should likely allow for up to at least 3 hours
        # before timing out, with a configurable parameter for the maximum timeout.
        if (
            db_config.use_blue_green
            and current_db_state
            and any(
                (
                    db_config.engine_version != current_db_state.get("EngineVersion"),
                    db_config.multi_az != current_db_state.get("MultiAZ"),
                    db_config.storage_type != current_db_state.get("StorageType"),
                    db_config.instance_size != current_db_state.get("DBInstanceClass"),
                )
            )
        ):
            db_instance_identifier = current_db_state.get("DBInstanceIdentifier")
            turn_off_deletion_protection(db_instance_identifier)
            deletion_protection_for_primary = False
            custom_timeouts = pulumi.CustomTimeouts(
                create=f"{db_config.blue_green_timeout_minutes}m",
                update=f"{db_config.blue_green_timeout_minutes}m",
                delete=f"{db_config.blue_green_timeout_minutes}m",
            )
            resource_options = pulumi.ResourceOptions.merge(
                resource_options,
                pulumi.ResourceOptions(custom_timeouts=custom_timeouts),
            )
            replica_options = pulumi.ResourceOptions.merge(
                resource_options,
                pulumi.ResourceOptions(
                    ignore_changes=["engineVersion", "parameterGroupName"]
                ),
            )

        if db_config.read_replica:
            replica_identifier = f"{db_config.instance_name}-replica"
            current_replica_state = get_rds_instance(replica_identifier)
            if (
                current_db_state
                and current_replica_state
                and db_config.engine_version
                not in (
                    current_db_state["EngineVersion"],
                    current_replica_state["EngineVersion"],
                )
                and engine_major_version(db_config.engine_version)
                == engine_major_version(current_db_state["EngineVersion"])
                and not db_config.use_blue_green
            ):
                # Keep the primary engine version pinned while the replica is upgraded
                # first.
                primary_engine_version = current_db_state["EngineVersion"]
                primary_parameter_group_family = parameter_group_family(
                    db_config.engine, primary_engine_version
                )

        if db_config.read_replica and db_config.engine == "postgres":
            # Necessary to allow for long-running sync queries from the replica
            # https://docs.airbyte.com/integrations/sources/postgres/#sync-data-from-postgres-hot-standby-server
            # (TMM 2022-11-02)
            db_config.parameter_overrides.append(
                {"name": "hot_standby_feedback", "value": 1}
            )

        # Remove rds.logical_replication from parameter overrides if updating an
        # existing parameter group that doesn't have it explicitly configured.
        # The parameter exists in all Postgres parameter groups, but we only care
        # if it was user-modified (not at default value). This parameter requires
        # a reboot, so we only apply it to:
        # - New instances (no current_db_state)
        # - Major version upgrades (new parameter group with different family)
        # - Existing instances where it's already user-configured
        if (
            db_config.engine == "postgres"
            and current_db_state
            and "rds.logical_replication"
            in [p["name"] for p in db_config.parameter_overrides]
        ):
            # Check if we're using the same parameter group
            # (not a major version upgrade)
            current_engine_version = current_db_state.get("EngineVersion")
            if current_engine_version:
                current_parameter_group_family = parameter_group_family(
                    db_config.engine, current_engine_version
                )
                if current_parameter_group_family == primary_parameter_group_family:
                    # Same parameter group - check if it's been user-configured
                    current_param_group_name = current_db_state.get(
                        "DBParameterGroups", [{}]
                    )[0].get("DBParameterGroupName")
                    if current_param_group_name:
                        existing_params = get_parameter_group_parameters(
                            current_param_group_name
                        )
                        if not any(
                            p["ParameterName"] == "rds.logical_replication"
                            for p in existing_params
                        ):
                            # Not user-configured - remove to avoid reboot
                            db_config.parameter_overrides = [
                                p
                                for p in db_config.parameter_overrides
                                if p["name"] != "rds.logical_replication"
                            ]

        self.parameter_group = primary_parameter_group = rds.ParameterGroup(
            f"{db_config.instance_name}-{db_config.engine}-parameter-group",
            family=primary_parameter_group_family,
            opts=resource_options,
            name_prefix=f"{db_config.instance_name}-{db_config.engine}-",
            tags=db_config.tags,
            parameters=db_config.parameter_overrides,
        )

        self.db_instance = rds.Instance(
            f"{db_config.instance_name}-{db_config.engine}-instance",
            allocated_storage=db_config.storage,
            allow_major_version_upgrade=True,
            auto_minor_version_upgrade=True,
            apply_immediately=True,
            backup_retention_period=db_config.backup_days,
            blue_green_update={"enabled": db_config.use_blue_green},
            copy_tags_to_snapshot=True,
            db_name=db_config.db_name,
            db_subnet_group_name=db_config.subnet_group_name,
            deletion_protection=deletion_protection_for_primary,
            engine=db_config.engine,
            engine_version=primary_engine_version,
            iam_database_authentication_enabled=db_config.enable_iam_auth,
            final_snapshot_identifier=(
                f"{db_config.instance_name}-{db_config.engine}-final-snapshot"
            ),
            identifier=db_config.instance_name,
            instance_class=db_config.instance_size,
            max_allocated_storage=db_config.max_storage,
            multi_az=db_config.multi_az,
            opts=resource_options,
            parameter_group_name=primary_parameter_group.name,
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
            if primary_parameter_group_family != replica_parameter_group_family:
                replica_parameter_group = rds.ParameterGroup(
                    f"{db_config.instance_name}-{db_config.engine}-replica-parameter-group",
                    family=replica_parameter_group_family,
                    opts=resource_options,
                    name_prefix=f"{db_config.instance_name}-{db_config.engine}-replica-",
                    tags=db_config.tags,
                    parameters=db_config.parameter_overrides,
                )
            else:
                replica_parameter_group = self.parameter_group
            self.db_replica = rds.Instance(
                f"{db_config.instance_name}-{db_config.engine}-replica",
                allow_major_version_upgrade=True,
                apply_immediately=True,
                auto_minor_version_upgrade=True,
                engine_version=replica_engine_version,
                identifier=replica_identifier,
                instance_class=db_config.read_replica.instance_size,
                kms_key_id=self.db_instance.kms_key_id,
                max_allocated_storage=db_config.max_storage,
                opts=replica_options,
                parameter_group_name=replica_parameter_group.name,
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
                    tags=db_config.tags,
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
                "threshold": 90,  # percent
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
                "threshold": 0.100,  # 100 milliseconds
            },
            "ReadLatency": {
                "comparison_operator": "GreaterThanThreshold",
                "description": "RDS - High Read Latency",
                "datapoints_to_alarm": 2,
                "level": "warning",
                "period": 300,  # 5 minutes
                "evaluation_periods": 2,  # 10 minutes
                "metric_name": "ReadLatency",
                "threshold": 0.020,  # 20 milliseconds
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
                    "threshold": 10,  # requests
                },
            },
        }
        return dict(
            **global_profiles,
            **(monitoring_profiles[profile_name]),
        )
