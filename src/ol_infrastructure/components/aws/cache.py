"""This module defines a Pulumi component resource for enforcing best practices for building AWS Elasticache clusters.

This includes:
- Create a parameter group for each deployed cluster
- Create a clustered deployment of Redis or Memcached
"""
import re

from typing import Any, Dict, List, Optional, Text

import pulumi

from pulumi_aws import elasticache
from pydantic import PositiveInt, conint, validator

from ol_infrastructure.lib.aws.elasticache_helper import (
    cache_engines,
    parameter_group_family
)
from ol_infrastructure.lib.ol_types import AWSBase


class OLAmazonCacheConfig(AWSBase):
    auto_upgrade: bool = True  # Automatically perform ugprades of minor versions
    apply_immediately: bool = False
    cluster_description: Text
    cluster_name: Text
    engine: Text
    engine_version: Text
    instance_type: Text
    num_instances: PositiveInt = PositiveInt(3)
    parameter_overrides: Optional[Dict[Text, Any]] = None
    port: int
    security_groups: List[Text]
    subnet_group: Text  # the name of the subnet group created in the OLVPC component resource

    @validator('engine')
    def is_valid_engine(cls: 'OLAmazonCacheConfig', engine: Text) -> Text:  # noqa: N805, D102
        valid_engines = cache_engines()
        if engine not in valid_engines:
            raise ValueError('The specified cache engine is not a valid option in AWS.')
        return engine

    @validator('engine_version')
    def is_valid_version(  # noqa: D102
            cls: 'OLAmazonCacheConfig',  # noqa: N805
            engine_version: Text,
            values: Dict  # noqa: WPS110
    ) -> Text:
        engine = values.get('engine')
        engines_map = cache_engines()
        if engine_version not in engines_map.get(engine, []):
            raise ValueError(f'The specified version of the {engine} engine is not supported in AWS.')
        return engine_version


class OLAmazonRedisConfig(OLAmazonCacheConfig):
    cluster_mode_enabled: bool = True
    encrypt_transit: bool = True
    auth_token: Optional[Text] = None
    encrypted: bool = True
    engine: Text = 'redis'
    engine_version: Text = '6.x'
    kms_key_id: Optional[Text] = None
    node_group_count: PositiveInt = PositiveInt(2)
    port: PositiveInt = PositiveInt(6379)
    replica_count: PositiveInt = PositiveInt(3)
    snapshot_retention_days: PositiveInt = PositiveInt(5)

    @validator('auth_token')
    def is_auth_token_valid(
            cls: 'OLAmazonRedisConfig',  # noqa: N805
            auth_token: Optional[Text],
            values: Dict  # noqa: WPS110
    ) -> Optional[Text]:
        if not values['encrypt_transit']:
            return auth_token
        if values['encrypt_transit'] and auth_token is None:
            raise ValueError('Cannot encrypt transit with no auth token configured')
        token_valid = 16 <= len(auth_token or '') <= 128 and bool(re.search('[^\'"/@\W]+', auth_token or ''))  # noqa: WPS221, W605, E501
        if not token_valid:
            raise ValueError('The configured auth token has invalid characters. '
                             'Only printable ASCII characters excluding ", / and @ are allowed.')
        return auth_token

    @validator('cluster_name')
    def is_valid_cluster_name(cls: 'OLAmazonRedisConfig', cluster_name: Text) -> Text:  # noqa: N805
        is_valid: bool = True
        is_valid = 1 < len(cluster_name) < 41
        is_valid = not bool(re.search('[^a-zA-Z-9-]', cluster_name))
        if not is_valid:
            raise ValueError('The cluster name does not comply with the Elasticache naming constraints for Redis')
        return cluster_name


class OLAmazonMemcachedConfig(OLAmazonCacheConfig):
    engine: Text = 'memcached'
    engine_version: Text = '1.5.16'
    port: PositiveInt = PositiveInt(11211)
    num_instances: conint(ge=1, le=20) = 3  # type: ignore

    @validator('cluster_name')
    def is_valid_cluster_name(cls: 'OLAmazonMemcachedConfig', cluster_name: Text) -> Text:  # noqa: N805
        is_valid: bool = True
        is_valid = 1 < len(cluster_name) < 51
        is_valid = not bool(re.search('[^a-zA-Z-9-]', cluster_name))
        if not is_valid:
            raise ValueError('The cluster name does not comply with the Elasticache naming constraints for Memcached')
        return cluster_name


class OLAmazonCache(pulumi.ComponentResource):

    def __init__(
            self,
            cache_config: OLAmazonCacheConfig,
            opts: pulumi.ResourceOptions = None
    ):
        resource_options = pulumi.ResourceOptions(parent=self).merge(opts)  # type: ignore
        super().__init__(
            'ol:infrastructure:aws:elasticache:OLAmazonCache',
            cache_config.cluster_name,
            None,
            resource_options
        )

        clustered_redis = cache_config.engine == 'redis' and cache_config.cluster_mode_enabled

        cache_parameters = [elasticache.ParameterGroupParameterArgs(name=name, value=setting)
                            for name, setting in (cache_config.parameter_overrides or {}).items()]
        if clustered_redis:
            cache_parameters.append(elasticache.ParameterGroupParameterArgs(name='cluster-enabled', value=True))

        self.parameter_group = elasticache.ParameterGroup(
            f'{cache_config.cluster_name}-{cache_config.engine}-{cache_config.engine_version}-parameter-group',
            name=f'{cache_config.cluster_name}-parameter-group',
            family=parameter_group_family(cache_config.engine, cache_config.engine_version),
            parameters=cache_parameters,
            opts=resource_options
        )

        if clustered_redis:
            self.cache_cluster = elasticache.ReplicationGroup(
                f'{cache_config.cluster_name}-{cache_config.engine}-elasticache-cluster',
                apply_immediately=cache_config.apply_immediately,
                at_rest_encryption_enabled=cache_config.encrypted,
                auth_token=cache_config.auth_token,
                auto_minor_version_upgrade=cache_config.auto_upgrade,
                automatic_failover_enabled=True,
                cluster_mode=elasticache.ReplicationGroupClusterModeArgs(
                    num_node_groups=cache_config.node_group_count,
                    replicas_per_node_group=cache_config.replica_count
                ),
                engine='redis',
                engine_version=cache_config.engine_version,
                kms_key_id=cache_config.kms_key_id,
                name=cache_config.cluster_name,
                node_type=cache_config.instance_type,
                number_cache_clusters=cache_config.num_instances,
                opts=resource_options,
                parameter_group_name=self.parameter_group.name,
                port=cache_config.port,
                replication_group_description=cache_config.cluster_description,
                security_group_ids=cache_config.security_groups,
                snapshot_retention_limit=cache_config.snapshot_retention_days,
                subnet_group_name=cache_config.subnet_group,
                tags=cache_config.tags,
                transit_encryption_enabled=cache_config.encrypt_transit,
            )
        else:
            self.cache_cluster = elasticache.Cluster(
                f'{cache_config.cluster_name}-{cache_config.engine}-elasticache-cluster',
                engine=cache_config.engine,
                name=cache_config.cluster_name,
                cluster_id=cache_config.cluster_name,
                opts=resource_options,
                engine_version=cache_config.engine_version,
                az_mode='cross-az' if cache_config.num_instances > 1 else 'single-az',
                apply_immediately=cache_config.apply_immediately,
                node_type=cache_config.instance_type,
                num_cache_nodes=cache_config.num_instances,
                parameter_group_name=self.parameter_group.name,
                port=cache_config.port,
                security_group_ids=cache_config.security_groups,
                subnet_group_name=cache_config.subnet_group,
                tags=cache_config.tags
            )

        self.register_outputs({
            'cache_cluster': self.cache_cluster.id,
            'parameter_group': self.parameter_group.name
        })
