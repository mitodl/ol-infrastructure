from collections import defaultdict
from functools import lru_cache

import boto3

cache_client = boto3.client("elasticache")


@lru_cache
def cache_engines() -> dict[str, list[str]]:
    """Generate a list of cache engines and their currently available versions
        on Elasticache.

    :returns: Dictionary of engine names and the list of available versions

    :rtype: Dict[str, List[str]]
    """
    all_engines_paginator = cache_client.get_paginator("describe_cache_engine_versions")
    engines_versions = defaultdict(list)
    for engines_page in all_engines_paginator.paginate():
        for engine in engines_page["CacheEngineVersions"]:
            engines_versions[engine["Engine"]].append(engine["EngineVersion"])
    return dict(engines_versions)


@lru_cache
def parameter_group_family(engine: str, engine_version: str) -> str:
    """Return the valid parameter group family for the specified cache
        engine and version.

    :param engine: Name of the cache engine (e.g. redis or memcached)
    :type engine: str

    :param engine_version: Version of the cache engine being used (e.g. 3.1)
    :type engine_version: str

    :returns: The name of the parameter group family for the specified engine
        and version.

    :rtype: str
    """
    engine_details = cache_client.describe_cache_engine_versions(
        Engine=engine, EngineVersion=engine_version
    )
    return engine_details["CacheEngineVersions"][0]["CacheParameterGroupFamily"]
