"""This module defines a Pulumi component resource for building ElastiCache
serverless caches.

This includes:
- Create a serverless Redis/ValKey cache deployment
- Configure security groups and subnet access
- Handle snapshots and retention policies
- Set usage limits for cost control
"""

import re
from typing import Any, Literal, Union

import pulumi
from pulumi_aws import elasticache
from pydantic import ConfigDict, PositiveInt, field_validator

from ol_infrastructure.lib.ol_types import AWSBase

PulumiString = Union[str | pulumi.Output[str]]  # noqa: UP007

# AWS ElastiCache serverless limits
MAX_SNAPSHOT_RETENTION_DAYS = 35


class OLAmazonServerlessCacheConfig(AWSBase):
    """Configuration for creating an ElastiCache serverless cache.

    Args:
        cache_name: Unique name for the cache (1-40 chars, alphanumeric and hyphens)
        description: Optional description of the cache
        engine: Cache engine ("valkey" or "redis"). Defaults to "valkey"
        major_engine_version: Major version of the engine (7 or 8). Defaults to 7
        kms_key_id: KMS key ID for encryption at rest
        security_group_ids: List of security group IDs for network access control
        subnet_ids: List of 2-3 subnet IDs in different AZs
        user_group_id: Optional user group ID for RBAC
        daily_snapshot_time: Daily snapshot window in HH:MM UTC format (e.g., "03:00")
        snapshot_retention_limit: Number of days to retain snapshots (0-35)
        max_data_storage_gb: Maximum data storage in GB (for cost control)
        max_ecpu_per_second: Maximum ECPUs per second (for cost control)
        tags: AWS resource tags
    """

    cache_name: str
    description: str | None = None
    engine: Literal["valkey", "redis"] = "valkey"
    major_engine_version: Literal[7, 8] = 7
    kms_key_id: str | None = None
    security_group_ids: list[PulumiString] | None = None
    subnet_ids: list[PulumiString] | pulumi.Output[Any] | None = None
    user_group_id: str | None = None
    daily_snapshot_time: str | None = None
    snapshot_retention_limit: PositiveInt | None = None
    max_data_storage_gb: PositiveInt | None = None
    max_ecpu_per_second: PositiveInt | None = None
    model_config = ConfigDict(arbitrary_types_allowed=True)

    @field_validator("cache_name")
    @classmethod
    def is_valid_cache_name(cls, cache_name: str) -> str:
        """Validate cache name meets ElastiCache serverless constraints."""
        cache_name_max_length = 40
        is_valid: bool = True
        is_valid = 1 <= len(cache_name) <= cache_name_max_length
        is_valid = not bool(re.search("[^a-zA-Z0-9-]", cache_name))
        if cache_name.startswith("-") or cache_name.endswith("-"):
            is_valid = False
        if not is_valid:
            msg = "The cache name does not comply with the ElastiCache serverless naming constraints"  # noqa: E501
            raise ValueError(msg)
        return cache_name

    @field_validator("daily_snapshot_time")
    @classmethod
    def validate_snapshot_time(cls, time_str: str | None) -> str | None:
        """Validate snapshot time is in HH:MM UTC format."""
        if time_str is None:
            return None
        if not re.match(r"^([01]\d|2[0-3]):[0-5]\d$", time_str):
            msg = "Snapshot time must be in HH:MM format (00:00-23:59 UTC)"
            raise ValueError(msg)
        return time_str

    @field_validator("snapshot_retention_limit")
    @classmethod
    def validate_retention_limit(cls, limit: PositiveInt | None) -> PositiveInt | None:
        """Validate snapshot retention is within AWS limits (0-35 days)."""
        if limit is not None and limit > MAX_SNAPSHOT_RETENTION_DAYS:
            msg = (
                f"Snapshot retention limit cannot exceed "
                f"{MAX_SNAPSHOT_RETENTION_DAYS} days"
            )
            raise ValueError(msg)
        return limit


class OLAmazonServerlessCache(pulumi.ComponentResource):
    """Pulumi component for creating an AWS ElastiCache serverless cache.

    This component creates a serverless ValKey or Redis cache with optional
    usage limits, snapshots, and network security configuration.

    Attributes:
        serverless_cache: The underlying ElastiCache ServerlessCache resource
        address: The endpoint address for connecting to the cache
        port: The port number for connecting to the cache
    """

    serverless_cache: elasticache.ServerlessCache
    address: pulumi.Output[str]
    port: pulumi.Output[int]

    def __init__(
        self,
        cache_config: OLAmazonServerlessCacheConfig,
        opts: pulumi.ResourceOptions | None = None,
    ):
        """Create a serverless ElastiCache instance.

        Args:
            cache_config: Configuration for the serverless cache
            opts: Additional Pulumi resource options
        """
        super().__init__(
            "ol:infrastructure:aws:elasticache:OLAmazonServerlessCache",
            cache_config.cache_name,
            None,
            opts,
        )
        resource_options = pulumi.ResourceOptions(parent=self).merge(opts)

        # Build usage limits for cost control if specified
        cache_usage_limits = None
        if cache_config.max_data_storage_gb or cache_config.max_ecpu_per_second:
            cache_usage_limits_args = {}
            if cache_config.max_data_storage_gb:
                cache_usage_limits_args["data_storage"] = {
                    "maximum": cache_config.max_data_storage_gb,
                    "unit": "GB",
                }
            if cache_config.max_ecpu_per_second:
                cache_usage_limits_args["ecpu_per_second"] = {
                    "maximum": cache_config.max_ecpu_per_second,
                }
            cache_usage_limits = cache_usage_limits_args

        # Create the serverless cache
        self.serverless_cache = elasticache.ServerlessCache(
            f"{cache_config.cache_name}-serverless-cache",
            name=cache_config.cache_name,
            engine=cache_config.engine,
            major_engine_version=str(cache_config.major_engine_version),
            description=cache_config.description,
            kms_key_id=cache_config.kms_key_id,
            security_group_ids=cache_config.security_group_ids,
            subnet_ids=cache_config.subnet_ids,
            user_group_id=cache_config.user_group_id,
            daily_snapshot_time=cache_config.daily_snapshot_time,
            snapshot_retention_limit=cache_config.snapshot_retention_limit,
            cache_usage_limits=cache_usage_limits,
            tags=cache_config.tags,
            opts=resource_options,
        )

        # Expose address and port for connection string building
        # ServerlessCache.endpoints returns a list of endpoint objects
        # In serverless mode, there's typically one primary endpoint
        self.address = self.serverless_cache.endpoints[0].address
        self.port = self.serverless_cache.endpoints[0].port

        self.register_outputs(
            {
                "serverless_cache": self.serverless_cache.arn,
                "endpoints": self.serverless_cache.endpoints,
                "address": self.address,
                "port": self.port,
            }
        )
