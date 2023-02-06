from enum import Enum, unique


@unique
class CacheInstanceTypes(str, Enum):
    small = "cache.t3.small"
    medium = "cache.t2.medium"
    large = "cache.m6g.large"
    xlarge = "cache.m6g.xlarge"
    high_mem_large = "cache.r6g.large"
    high_mem_xlarge = "cache.r6g.xlarge"
