"""This module defines a Pulumi component resource for building Elasticache clusters.

This includes:
- Create a parameter group for each deployed cluster
- Create a clustered deployment of Redis or Memcached
"""
import re
from typing import Any, Dict, List, Optional, Union

import pulumi
from pulumi_aws import elasticache
from pydantic import PositiveInt, conint, validator

from bridge.lib.magic_numbers import DEFAULT_MEMCACHED_PORT, DEFAULT_REDIS_PORT
from ol_infrastructure.lib.aws.elasticache_helper import (
    cache_engines,
    parameter_group_family,
)
from ol_infrastructure.lib.ol_types import AWSBase

PulumiString = Union[str, pulumi.Output[str]]
MAX_MEMCACHED_CLUSTER_SIZE = 20


class OLAmazonCacheConfig(AWSBase):
    encrypt_transit: Optional[bool] = None
    encrypted: Optional[bool] = None
    auth_token: Optional[str] = None
    shard_count: Optional[int] = None
    kms_key_id: Optional[str] = None
    snapshot_retention_days: Optional[int] = None
    auto_upgrade: bool = True  # Automatically perform ugprades of minor versions
    apply_immediately: bool = False
    cluster_description: str
    cluster_name: str
    engine: str
    engine_version: str
    instance_type: str
    num_instances: PositiveInt = PositiveInt(3)
    parameter_overrides: Optional[Dict[str, Any]] = None
    port: int
    security_groups: List[PulumiString]
    subnet_group: Union[
        str, pulumi.Output[str]
    ]  # the name of the subnet group created in the OLVPC component

    class Config:
        arbitrary_types_allowed = True

    @validator("engine")
    def is_valid_engine(cls: "OLAmazonCacheConfig", engine: str) -> str:  # noqa: N805
        valid_engines = cache_engines()
        if engine not in valid_engines:
            raise ValueError("The specified cache engine is not a valid option in AWS.")
        return engine

    @validator("engine_version")
    def is_valid_version(
        cls: "OLAmazonCacheConfig",  # noqa: N805
        engine_version: str,
        values: Dict,  # noqa: WPS110
    ) -> str:
        engine: str = values.get("engine")
        engines_map = cache_engines()
        if engine_version not in engines_map.get(engine, []):
            raise ValueError(
                f"The specified version of the {engine} engine is not supported in AWS."
            )
        return engine_version


class OLAmazonRedisConfig(OLAmazonCacheConfig):
    cluster_mode_enabled: bool
    encrypt_transit: bool = True
    auth_token: Optional[PulumiString] = None
    encrypted: bool = True
    engine: str = "redis"
    engine_version: str = "6.x"
    kms_key_id: Optional[PulumiString] = None
    num_instances: conint(ge=1, le=5) = 1  # type: ignore
    shard_count: PositiveInt = PositiveInt(1)
    port: PositiveInt = PositiveInt(DEFAULT_REDIS_PORT)
    snapshot_retention_days: PositiveInt = PositiveInt(5)

    @validator("auth_token")
    def is_auth_token_valid(
        cls: "OLAmazonRedisConfig",  # noqa: N805
        auth_token: Optional[str],
        values: Dict,  # noqa: WPS110
    ) -> Optional[str]:
        min_token_length = 16
        max_token_length = 128
        if not values["encrypt_transit"]:
            return auth_token
        if values["encrypt_transit"] and auth_token is None:
            raise ValueError("Cannot encrypt transit with no auth token configured")
        token_valid = min_token_length <= len(
            auth_token or ""
        ) <= max_token_length and bool(re.search(r"[^'\"/@\\W]+", auth_token or ""))
        if not token_valid:
            raise ValueError(
                "The configured auth token has invalid characters. "
                'Only printable ASCII characters excluding ", / and @ are allowed.'
            )
        return auth_token

    @validator("cluster_name")
    def is_valid_cluster_name(
        cls: "OLAmazonRedisConfig", cluster_name: str  # noqa: N805
    ) -> str:
        cluster_name_max_length = 41
        is_valid: bool = True
        is_valid = 1 < len(cluster_name) < cluster_name_max_length
        is_valid = not bool(re.search("[^a-zA-Z-9-]", cluster_name))
        if not is_valid:
            raise ValueError(
                "The cluster name does not comply with the Elasticache naming "
                "constraints for Redis"
            )
        return cluster_name


class OLAmazonMemcachedConfig(OLAmazonCacheConfig):
    engine: str = "memcached"
    engine_version: str = "1.5.16"
    port: PositiveInt = PositiveInt(DEFAULT_MEMCACHED_PORT)
    num_instances: conint(ge=1, le=MAX_MEMCACHED_CLUSTER_SIZE) = 3  # type: ignore

    @validator("cluster_name")
    def is_valid_cluster_name(
        cls: "OLAmazonMemcachedConfig", cluster_name: str  # noqa: N805
    ) -> str:
        max_cluster_name_length = 51
        is_valid: bool = True
        is_valid = 1 < len(cluster_name) < max_cluster_name_length
        is_valid = not bool(re.search("[^a-zA-Z-9-]", cluster_name))
        if not is_valid:
            raise ValueError(
                "The cluster name does not comply with the Elasticache naming "
                "constraints for Memcached"
            )
        return cluster_name


class OLAmazonCache(pulumi.ComponentResource):
    def __init__(
        self,
        cache_config: Union[OLAmazonRedisConfig, OLAmazonMemcachedConfig],
        opts: pulumi.ResourceOptions = None,
    ):
        super().__init__(
            "ol:infrastructure:aws:elasticache:OLAmazonCache",
            cache_config.cluster_name,
            None,
            opts,
        )
        resource_options = pulumi.ResourceOptions(parent=self).merge(
            opts
        )  # type: ignore

        clustered_redis = cache_config.engine == "redis" and getattr(  # noqa: B009
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

        self.parameter_group = elasticache.ParameterGroup(
            f"{cache_config.cluster_name}-{cache_config.engine}-{cache_config.engine_version}-parameter-group",  # noqa: E501
            name=f"{cache_config.cluster_name}-parameter-group",
            family=parameter_group_family(
                cache_config.engine, cache_config.engine_version
            ),
            parameters=cache_parameters,
            opts=resource_options,
        )

        if cache_config.engine == "redis":
            if cache_config.engine_version.startswith("6"):
                cache_options = resource_options.merge(
                    pulumi.ResourceOptions(ignore_changes=["engine_version"])
                )  # type: ignore
            else:
                cache_options = resource_options
            cache_cluster, address = self.redis(
                cache_config,
                clustered_redis,
                cache_options,
            )
        else:
            cache_cluster, address = self.memcached(cache_config, resource_options)

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
        cluster_mode: bool,
        resource_options: pulumi.ResourceOptions,
    ):
        if cluster_mode:
            cluster_mode_param = elasticache.ReplicationGroupClusterModeArgs(
                num_node_groups=cache_config.shard_count,
                replicas_per_node_group=cache_config.num_instances,
            )
            cache_node_count = None
        else:
            cluster_mode_param = None  # type: ignore
            cache_node_count = cache_config.num_instances

        cache_cluster = elasticache.ReplicationGroup(
            f"{cache_config.cluster_name}-{cache_config.engine}-elasticache-cluster",
            apply_immediately=cache_config.apply_immediately,
            at_rest_encryption_enabled=cache_config.encrypted,
            auth_token=cache_config.auth_token,
            auto_minor_version_upgrade=cache_config.auto_upgrade,
            automatic_failover_enabled=True,
            cluster_mode=cluster_mode_param,
            engine="redis",
            engine_version=cache_config.engine_version,
            kms_key_id=cache_config.kms_key_id,
            node_type=cache_config.instance_type,
            number_cache_clusters=cache_node_count,
            opts=resource_options,
            parameter_group_name=self.parameter_group.name,
            port=cache_config.port,
            replication_group_description=cache_config.cluster_description,
            replication_group_id=cache_config.cluster_name,
            security_group_ids=cache_config.security_groups,
            snapshot_retention_limit=cache_config.snapshot_retention_days,
            subnet_group_name=cache_config.subnet_group,
            tags=cache_config.tags,
            transit_encryption_enabled=cache_config.encrypt_transit,
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
