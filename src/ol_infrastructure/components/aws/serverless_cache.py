"""This module defines a Pulumi component resource for building ElastiCache
serverless caches.

This includes:
- Create a serverless Redis/ValKey cache deployment with IAM authentication
- Configure security groups and subnet access
- Handle snapshots and retention policies
"""

import re
from typing import Union

import pulumi
from pulumi_aws import elasticache
from pydantic import ConfigDict, field_validator

from ol_infrastructure.lib.ol_types import AWSBase

PulumiString = Union[str | pulumi.Output[str]]  # noqa: UP007


class OLAmazonServerlessCacheConfig(AWSBase):
    cache_name: str
    description: str | None = None
    kms_key_id: str | None = None
    security_group_ids: list[PulumiString] | None = None
    subnet_ids: list[PulumiString] | pulumi.Output | None = None
    user_group_id: str | None = None
    daily_snapshot_time: str | None = None
    snapshot_retention_limit: int | None = None
    engine: str = "valkey"
    major_engine_version: int = 8
    model_config = ConfigDict(arbitrary_types_allowed=True)

    @field_validator("cache_name")
    @classmethod
    def is_valid_cache_name(cls, cache_name: str) -> str:
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


class OLAmazonServerlessCache(pulumi.ComponentResource):
    def __init__(
        self,
        cache_config: OLAmazonServerlessCacheConfig,
        opts: pulumi.ResourceOptions | None = None,
    ):
        super().__init__(
            "ol:infrastructure:aws:elasticache:OLAmazonServerlessCache",
            cache_config.cache_name,
            None,
            opts,
        )
        resource_options = pulumi.ResourceOptions(parent=self).merge(opts)

        # Create the serverless cache
        # Note: ServerlessCache uses IAM authentication, not auth_token
        self.serverless_cache = elasticache.ServerlessCache(
            f"{cache_config.cache_name}-serverless-cache",
            name=cache_config.cache_name,
            engine=cache_config.engine,
            major_engine_version=cache_config.major_engine_version,
            description=cache_config.description,
            kms_key_id=cache_config.kms_key_id,
            security_group_ids=cache_config.security_group_ids,
            subnet_ids=cache_config.subnet_ids,
            user_group_id=cache_config.user_group_id,
            daily_snapshot_time=cache_config.daily_snapshot_time,
            snapshot_retention_limit=cache_config.snapshot_retention_limit,
            tags=cache_config.tags,
            opts=resource_options,
        )

        # The serverless cache provides endpoints for read and write operations
        self.endpoint = self.serverless_cache.endpoints[0]
        self.reader_endpoint = self.serverless_cache.reader_endpoints[0]

        self.register_outputs(
            {
                "serverless_cache": self.serverless_cache.arn,
                "endpoint": self.endpoint,
                "reader_endpoint": self.reader_endpoint,
            }
        )