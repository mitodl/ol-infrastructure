from enum import Enum, unique


@unique
class CacheInstanceTypes(str, Enum):
    small = "cache.t4g.small"
    medium = "cache.t4g.medium"
    large = "cache.m7g.large"
    xlarge = "cache.m7g.xlarge"
    high_mem_large = "cache.r7g.large"
    high_mem_xlarge = "cache.r7g.xlarge"
