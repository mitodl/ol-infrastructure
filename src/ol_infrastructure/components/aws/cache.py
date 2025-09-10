"""This module defines a Pulumi component resource for building Elasticache clusters.

This includes:
- Create a parameter group for each deployed cluster
- Create a clustered deployment of Redis or Memcached
"""

import re
from typing import Any, Literal, Union

import pulumi
from pulumi_aws import elasticache
from pydantic import (
    ConfigDict,
    PositiveInt,
    ValidationInfo,
    conint,
    field_validator,
)
from pydantic.functional_validators import model_validator

from bridge.lib.magic_numbers import DEFAULT_MEMCACHED_PORT, DEFAULT_REDIS_PORT
from ol_infrastructure.components.aws.cloudwatch import (
    OLCloudWatchAlarmSimpleElastiCache,
    OLCloudWatchAlarmSimpleElastiCacheConfig,
)
from ol_infrastructure.lib.aws.elasticache_helper import (
    cache_engines,
    parameter_group_family,
)
from ol_infrastructure.lib.ol_types import AWSBase

PulumiString = Union[str | pulumi.Output[str]]  # noqa: UP007
MAX_MEMCACHED_CLUSTER_SIZE = 20


class OLAmazonCacheConfig(AWSBase):
    encrypt_transit: bool | None = None
    encrypted: bool | None = None
    auth_token: str | None = None
    shard_count: int | None = None
    kms_key_id: str | None = None
    snapshot_retention_days: int | None = None
    auto_upgrade: bool = True  # Automatically perform ugprades of minor versions
    apply_immediately: bool = True
    cluster_description: str
    cluster_name: str
    engine: str
    engine_version: str
    instance_type: str
    monitoring_profile_name: str = "ci"
    num_instances: PositiveInt = PositiveInt(3)
    parameter_overrides: dict[str, Any] | None = None
    port: int
    security_groups: list[PulumiString]
    subnet_group: (
        str | pulumi.Output[str]
    )  # the name of the subnet group created in the OLVPC component
    model_config = ConfigDict(arbitrary_types_allowed=True)

    @field_validator("engine")
    @classmethod
    def is_valid_engine(cls, engine: str) -> str:
        valid_engines = cache_engines()
        if engine not in valid_engines:
            msg = "The specified cache engine is not a valid option in AWS."
            raise ValueError(msg)
        return engine

    @field_validator("engine_version")
    @classmethod
    def is_valid_version(cls, engine_version: str, info: ValidationInfo) -> str:
        engine = info.data["engine"]
        engines_map = cache_engines()
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


class OLAmazonRedisConfig(OLAmazonCacheConfig):
    cluster_mode_enabled: bool
    encrypt_transit: bool = True
    auth_token: PulumiString | None = None
    encrypted: bool = True
    engine: Literal["redis", "valkey"] = "valkey"
    engine_version: str = "8.0"
    kms_key_id: PulumiString | None = None
    num_instances: conint(ge=1, le=5) = 1  # type: ignore  # noqa: PGH003
    shard_count: PositiveInt = PositiveInt(1)
    port: PositiveInt = PositiveInt(DEFAULT_REDIS_PORT)
    snapshot_retention_days: PositiveInt = PositiveInt(5)

    @field_validator("auth_token")
    @classmethod
    def is_auth_token_valid(
        cls, auth_token: str | None, info: ValidationInfo
    ) -> str | None:
        encrypt_transit = info.data["encrypt_transit"]
        min_token_length = 16
        max_token_length = 128
        if not encrypt_transit:
            return auth_token
        if encrypt_transit and auth_token is None:
            msg = "Cannot encrypt transit with no auth token configured"
            raise ValueError(msg)
        token_valid = min_token_length <= len(
            auth_token or ""
        ) <= max_token_length and bool(re.search(r"[^'\"/@\\W]+", auth_token or ""))
        if not token_valid:
            msg = 'The configured auth token has invalid characters. Only printable ASCII characters excluding ", / and @ are allowed.'  # noqa: E501
            raise ValueError(msg)
        return auth_token

    @field_validator("cluster_name")
    @classmethod
    def is_valid_cluster_name(cls, cluster_name: str) -> str:
        cluster_name_max_length = 41
        is_valid: bool = True
        is_valid = 1 < len(cluster_name) < cluster_name_max_length
        is_valid = not bool(re.search("[^a-zA-Z-9-]", cluster_name))
        if not is_valid:
            msg = "The cluster name does not comply with the Elasticache naming constraints for Redis"  # noqa: E501
            raise ValueError(msg)
        return cluster_name

    @model_validator(mode="after")
    def ensure_maxmemory_policy(self):
        if self.parameter_overrides is None:
            self.parameter_overrides = {}
        self.parameter_overrides["maxmemory-policy"] = self.parameter_overrides.get(
            "maxmemory-policy", "allkeys-lru"
        )
        return self


class OLAmazonMemcachedConfig(OLAmazonCacheConfig):
    engine: str = "memcached"
    engine_version: str = "1.5.16"
    port: PositiveInt = PositiveInt(DEFAULT_MEMCACHED_PORT)
    num_instances: conint(ge=1, le=MAX_MEMCACHED_CLUSTER_SIZE) = 3  # type: ignore[valid-type]

    @field_validator("cluster_name")
    @classmethod
    def is_valid_cluster_name(cls, cluster_name: str) -> str:
        max_cluster_name_length = 51
        is_valid: bool = True
        is_valid = 1 < len(cluster_name) < max_cluster_name_length
        is_valid = not bool(re.search("[^a-zA-Z-9-]", cluster_name))
        if not is_valid:
            msg = "The cluster name does not comply with the Elasticache naming constraints for Memcached"  # noqa: E501
            raise ValueError(msg)
        return cluster_name


class OLAmazonCache(pulumi.ComponentResource):
    def __init__(
        self,
        cache_config: OLAmazonRedisConfig | OLAmazonMemcachedConfig,
        opts: pulumi.ResourceOptions | None = None,
    ):
        super().__init__(
            "ol:infrastructure:aws:elasticache:OLAmazonCache",
            cache_config.cluster_name,
            None,
            opts,
        )
        resource_options = pulumi.ResourceOptions(parent=self).merge(opts)

        clustered_redis = cache_config.engine in ("redis", "valkey") and getattr(  # noqa: B009
            cache_config, "cluster_mode_enabled"
        )

        cache_parameters = [
            elasticache.ParameterGroupParameterArgs(name=name, value=setting)
            for name, setting in (cache_config.parameter_overrides or {}).items()
        ]
        if clustered_redis:
            cache_parameters.append(
                elasticache.ParameterGroupParameterArgs(
                    name="cluster-enabled", value="yes"
                )
            )
        cluster_name = cache_config.cluster_name
        engine_version = cache_config.engine_version
        self.parameter_group = elasticache.ParameterGroup(
            f"{cluster_name}-{cache_config.engine}-{engine_version}-parameter-group",
            name=f"{cluster_name}-{engine_version.replace('.', '')}-parameter-group",
            family=parameter_group_family(
                cache_config.engine, cache_config.engine_version
            ),
            parameters=cache_parameters,
            opts=resource_options,
            tags=cache_config.tags,
        )

        if cache_config.engine in ("redis", "valkey"):
            if cache_config.engine_version.startswith("6"):
                cache_options = resource_options.merge(
                    pulumi.ResourceOptions(ignore_changes=["engine_version"])
                )
            else:
                cache_options = resource_options
            cache_cluster, address = self.redis(
                cache_config,
                clustered_redis,
                cache_options,
            )
        else:
            cache_cluster, address = self.memcached(cache_config, resource_options)

        monitoring_profile = self._get_default_monitoring_profile(
            cache_config.monitoring_profile_name, cache_config.engine
        )
        for alarm_name, alarm_args in monitoring_profile.items():
            for i in range(1, cache_config.num_instances + 1):
                node_id_suffix = f"-00{i}"
                alarm_config = OLCloudWatchAlarmSimpleElastiCacheConfig(
                    cluster_id=cache_config.cluster_name,
                    node_id=node_id_suffix,
                    name=f"{cache_config.cluster_name}{node_id_suffix}-{alarm_name}-OLCloudWatchAlarmSimpleElastiCacheConfig",
                    tags=cache_config.tags,
                    **alarm_args,
                )
                OLCloudWatchAlarmSimpleElastiCache(alarm_config=alarm_config)

        self.cache_cluster = cache_cluster
        self.address = address

        self.register_outputs(
            {
                "cache_cluster": self.cache_cluster.id,
                "parameter_group": self.parameter_group.name,
            }
        )

    def redis(
        self,
        cache_config: OLAmazonRedisConfig,
        cluster_mode: bool,  # noqa: FBT001
        resource_options: pulumi.ResourceOptions,
    ):
        if cluster_mode:
            cluster_kwargs = {
                "num_cache_clusters": None,
                "num_node_groups": cache_config.shard_count,
                "replicas_per_node_group": cache_config.num_instances,
            }
        else:
            cluster_kwargs = {
                "num_cache_clusters": cache_config.num_instances,
                "num_node_groups": None,
                "replicas_per_node_group": None,
            }

        cache_cluster = elasticache.ReplicationGroup(
            f"{cache_config.cluster_name}-{cache_config.engine}-elasticache-cluster",
            apply_immediately=cache_config.apply_immediately,
            at_rest_encryption_enabled=cache_config.encrypted,
            auth_token=cache_config.auth_token,
            auto_minor_version_upgrade=cache_config.auto_upgrade,
            automatic_failover_enabled=True,
            engine=cache_config.engine,
            engine_version=(
                "6.x"
                if cache_config.engine_version.startswith("6")
                else cache_config.engine_version
            ),
            kms_key_id=cache_config.kms_key_id,
            node_type=cache_config.instance_type,
            opts=resource_options,
            parameter_group_name=self.parameter_group.name,
            port=cache_config.port,
            description=cache_config.cluster_description,
            replication_group_id=cache_config.cluster_name,
            security_group_ids=cache_config.security_groups,
            snapshot_retention_limit=cache_config.snapshot_retention_days,
            subnet_group_name=cache_config.subnet_group,
            tags=cache_config.tags,
            transit_encryption_enabled=cache_config.encrypt_transit,
            **cluster_kwargs,
        )
        address = (
            cache_cluster.configuration_endpoint_address
            if cluster_mode
            else cache_cluster.primary_endpoint_address
        )
        return (cache_cluster, address)

    def memcached(
        self,
        cache_config: OLAmazonMemcachedConfig,
        resource_options: pulumi.ResourceOptions,
    ):
        cache_cluster = elasticache.Cluster(
            f"{cache_config.cluster_name}-{cache_config.engine}-elasticache-cluster",
            engine=cache_config.engine,
            cluster_id=cache_config.cluster_name,
            opts=resource_options,
            engine_version=cache_config.engine_version,
            az_mode="cross-az" if cache_config.num_instances > 1 else "single-az",
            apply_immediately=cache_config.apply_immediately,
            node_type=cache_config.instance_type,
            num_cache_nodes=cache_config.num_instances,
            parameter_group_name=self.parameter_group.name,
            port=cache_config.port,
            security_group_ids=cache_config.security_groups,
            subnet_group_name=cache_config.subnet_group,
            tags=cache_config.tags,
        )
        address = cache_cluster.member_clusters
        return (cache_cluster, address)

    def _get_default_monitoring_profile(self, profile_name: str, engine: str):
        if profile_name == "disabled":
            return {}
        if engine in ("redis", "valkey"):
            return self._get_default_redis_monitoring_profile(profile_name)
        elif engine == "memcached":
            return self._get_default_memcached_monitoring_profile(profile_name)
        else:
            msg = f"Invalid engine specifier: {engine}"
            raise ValueError(msg)

    def _get_default_memcached_monitoring_profile(
        self,
        profile_name: str,  # noqa: ARG002
    ):  # noqa: ARG002, RUF100
        return {}  # not implemented

    def _get_default_redis_monitoring_profile(self, profile_name: str):
        global_profiles = {
            "EngineCPUUtilization": {
                "comparison_operator": "GreaterThanThreshold",
                "description": (
                    "ElastiCache - High CPU utilization by the Redis engine."
                ),
                "datapoints_to_alarm": 2,
                "level": "warning",
                "period": 300,  # 5 minutes
                "evaluation_periods": 2,  # 10 Minutes
                "metric_name": "EngineCPUUtilization",
                "threshold": 50,  # percent
                "unit": "Percent",
            },
            "DatabaseMemoryUsagePercentage": {
                "comparison_operator": "GreaterThanThreshold",
                "description": (
                    "ElastiCache - High memory utilization by the Redis engine."
                ),
                "datapoints_to_alarm": 2,
                "level": "warning",
                "period": 300,  # 5 minutes
                "evaluation_periods": 2,  # 10 Minutes
                "metric_name": "DatabaseMemoryUsagePercentage",
                "threshold": 90,  # percent
            },
        }

        monitoring_profiles: dict[str, dict[str, Any]] = {
            "ci": {},
            "qa": {},
            "production": {},
        }
        return dict(**global_profiles, **(monitoring_profiles[profile_name]))
