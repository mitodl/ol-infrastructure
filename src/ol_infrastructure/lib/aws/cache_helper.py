"""Helper module for creating Redis/ValKey caches with flexible backend selection.

This module provides a unified interface for creating either dedicated (OLAmazonCache)
or serverless (OLAmazonServerlessCache) caches based on configuration, making it easy
to migrate environments to serverless while maintaining compatibility.
"""

from enum import Enum, unique
from typing import Any, Literal

import pulumi
from pulumi import Config, ResourceOptions

from ol_infrastructure.components.aws.cache import OLAmazonCache, OLAmazonRedisConfig
from ol_infrastructure.components.aws.serverless_cache import (
    OLAmazonServerlessCache,
    OLAmazonServerlessCacheConfig,
)
from ol_infrastructure.lib.pulumi_helper import StackInfo


@unique
class CacheInstanceTypes(str, Enum):
    """Enum of ElastiCache instance types for dedicated caches."""

    micro = "cache.t4g.micro"
    small = "cache.t4g.small"
    medium = "cache.t4g.medium"
    large = "cache.m7g.large"
    xlarge = "cache.m7g.xlarge"
    high_mem_large = "cache.r7g.large"
    high_mem_xlarge = "cache.r7g.xlarge"


# Type alias for cache backends
type CacheBackend = OLAmazonCache | OLAmazonServerlessCache


def create_redis_cache(  # noqa: PLR0913
    stack_info: StackInfo,
    cache_name: str,
    description: str,
    security_group_ids: list[Any],
    subnet_group: str | None = None,
    subnet_ids: list[Any] | pulumi.Output[Any] | None = None,
    auth_token: str | None = None,
    engine: Literal["redis", "valkey"] = "valkey",
    engine_version: str = "7.2",
    instance_type: str | None = None,
    num_instances: int = 3,
    tags: dict[str, str] | None = None,
    opts: ResourceOptions | None = None,
    config_key: str = "redis",
) -> OLAmazonCache | OLAmazonServerlessCache:
    """Create a Redis/ValKey cache with configurable backend (dedicated or serverless).

    This function creates either a dedicated ElastiCache cluster or a serverless cache
    based on the Pulumi configuration. It defaults to:
    - Serverless for CI and QA environments
    - Dedicated for Production and Staging environments

    Configuration can be overridden using the `use_serverless_cache` boolean in the
    specified config section (defaults to "redis").

    Args:
        stack_info: Stack information with environment details
        cache_name: Name for the cache cluster
        description: Description of the cache purpose
        security_group_ids: List of security group IDs for network access
        subnet_group: Subnet group name (for dedicated caches)
        subnet_ids: List of subnet IDs (for serverless caches)
        auth_token: Authentication token for Redis (dedicated only)
        engine: Cache engine ("valkey" or "redis")
        engine_version: Engine version string (e.g., "7.2")
        instance_type: Instance type for dedicated cache (e.g., "cache.t4g.small")
        num_instances: Number of instances for dedicated cache
        tags: AWS resource tags
        opts: Additional Pulumi resource options
        config_key: Configuration section key (default: "redis")

    Returns:
        Either OLAmazonCache or OLAmazonServerlessCache instance with .address property

    Example:
        ```python
        cache = create_redis_cache(
            stack_info=stack_info,
            cache_name=f"myapp-redis-{stack_info.env_suffix}",
            description="Redis cache for MyApp",
            security_group_ids=[redis_sg.id],
            subnet_group=vpc["elasticache_subnet"],
            subnet_ids=vpc["subnet_ids"][:3],
            auth_token=redis_config.require("password"),
            tags=aws_config.tags,
        )

        # Use the cache (works for both backends)
        redis_url = cache.address.apply(
            lambda addr: f"rediss://:{password}@{addr}:6379"
        )
        ```

    Configuration:
        # Pulumi.<stack>.yaml
        config:
          redis:use_serverless_cache: true  # Force serverless
          redis:max_data_storage_gb: 50  # Serverless limit
          redis:max_ecpu_per_second: 1000  # Serverless limit
    """
    redis_config = Config(config_key)
    tags = tags or {}

    # Determine if serverless should be used
    # Default: serverless for CI/QA, dedicated for Production/Staging
    default_use_serverless = stack_info.env_suffix.lower() in ("ci", "qa")
    use_serverless = redis_config.get_bool("use_serverless_cache") or (
        redis_config.get_bool("use_serverless_cache") is None and default_use_serverless
    )

    if use_serverless:
        return _create_serverless_cache(
            stack_info=stack_info,
            cache_name=cache_name,
            description=description,
            security_group_ids=security_group_ids,
            subnet_ids=subnet_ids,
            engine=engine,
            engine_version=engine_version,
            auth_token=auth_token,  # Pass auth token
            tags=tags,
            opts=opts,
            config_key=config_key,
        )
    else:
        return _create_dedicated_cache(
            stack_info=stack_info,
            cache_name=cache_name,
            description=description,
            security_group_ids=security_group_ids,
            subnet_group=subnet_group,
            auth_token=auth_token,
            engine=engine,
            engine_version=engine_version,
            instance_type=instance_type,
            num_instances=num_instances,
            tags=tags,
            opts=opts,
            config_key=config_key,
        )


def _create_serverless_cache(  # noqa: PLR0913
    stack_info: StackInfo,  # noqa: ARG001
    cache_name: str,
    description: str,
    security_group_ids: list[Any],
    subnet_ids: list[Any] | pulumi.Output[Any] | None,
    engine: str,
    engine_version: str,
    auth_token: str | None,
    tags: dict[str, str],
    opts: ResourceOptions | None,
    config_key: str,
) -> OLAmazonServerlessCache:
    """Create a serverless ElastiCache instance."""
    redis_config = Config(config_key)

    # Parse major version from version string (e.g., "7.2" -> 7)
    major_version = int(engine_version.split(".")[0])

    # Get serverless-specific configuration
    max_data_storage_gb = redis_config.get_int("max_data_storage_gb")
    max_ecpu_per_second = redis_config.get_int("max_ecpu_per_second")
    daily_snapshot_time = redis_config.get("daily_snapshot_time")
    snapshot_retention_limit = redis_config.get_int("snapshot_retention_limit")

    # Ensure subnet_ids is provided
    if subnet_ids is None:
        msg = (
            "subnet_ids is required for serverless cache. "
            "Pass subnet_ids=vpc['subnet_ids'][:3]"
        )
        raise ValueError(msg)

    serverless_config = OLAmazonServerlessCacheConfig(
        cache_name=f"{cache_name}-serverless",
        description=f"{description} (serverless)",
        engine=engine,
        major_engine_version=major_version,
        security_group_ids=security_group_ids,
        subnet_ids=subnet_ids,
        auth_token=auth_token,  # Pass auth token for authentication
        max_data_storage_gb=max_data_storage_gb,
        max_ecpu_per_second=max_ecpu_per_second,
        daily_snapshot_time=daily_snapshot_time,
        snapshot_retention_limit=snapshot_retention_limit,
        tags=tags,
    )

    return OLAmazonServerlessCache(serverless_config, opts=opts)


def _create_dedicated_cache(  # noqa: PLR0913
    stack_info: StackInfo,  # noqa: ARG001
    cache_name: str,
    description: str,
    security_group_ids: list[Any],
    subnet_group: str | None,
    auth_token: str | None,
    engine: str,
    engine_version: str,
    instance_type: str | None,
    num_instances: int,
    tags: dict[str, str],
    opts: ResourceOptions | None,
    config_key: str,
) -> OLAmazonCache:
    """Create a dedicated ElastiCache cluster."""
    redis_config = Config(config_key)

    # Get dedicated-specific configuration with defaults
    cluster_mode_enabled = redis_config.get_bool("cluster_mode_enabled") or False
    encrypted = redis_config.get_bool("encrypted")
    if encrypted is None:
        encrypted = True
    encrypt_transit = redis_config.get_bool("encrypt_transit")
    if encrypt_transit is None:
        encrypt_transit = True

    # Ensure subnet_group is provided
    if subnet_group is None:
        msg = (
            "subnet_group is required for dedicated cache. "
            "Pass subnet_group=vpc['elasticache_subnet']"
        )
        raise ValueError(msg)

    # Get instance type from config or parameter
    if instance_type is None:
        instance_type = redis_config.get("instance_type") or "cache.t4g.small"

    dedicated_config = OLAmazonRedisConfig(
        cluster_name=cache_name,
        cluster_description=description,
        cluster_mode_enabled=cluster_mode_enabled,
        engine=engine,
        engine_version=engine_version,
        instance_type=instance_type,
        num_instances=num_instances,
        security_groups=security_group_ids,
        subnet_group=subnet_group,
        auth_token=auth_token,
        encrypted=encrypted,
        encrypt_transit=encrypt_transit,
        port=6379,
        tags=tags,
    )

    return OLAmazonCache(dedicated_config, opts=opts)


def get_cache_config_summary(stack_info: StackInfo, config_key: str = "redis") -> str:
    """Get a summary of cache configuration for logging/output.

    Args:
        stack_info: Stack information
        config_key: Configuration section key

    Returns:
        Human-readable summary string
    """
    redis_config = Config(config_key)
    default_use_serverless = stack_info.env_suffix.lower() in ("ci", "qa")
    use_serverless = redis_config.get_bool("use_serverless_cache")

    if use_serverless is None:
        backend = "serverless (auto)" if default_use_serverless else "dedicated (auto)"
    else:
        backend = "serverless" if use_serverless else "dedicated"

    return f"Cache backend: {backend} for {stack_info.env_suffix} environment"
