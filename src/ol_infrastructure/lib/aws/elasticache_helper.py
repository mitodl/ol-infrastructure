from collections import defaultdict
from functools import lru_cache
from typing import Dict, List, Text

import boto3

cache_client = boto3.client("elasticache")


@lru_cache
def cache_engines() -> Dict[Text, List[Text]]:
    """Generate a list of cache engines and their currently available versions on Elasticache.

    :returns: Dictionary of engine names and the list of available versions

    :rtype: Dict[Text, List[Text]]
    """
    all_engines_paginator = cache_client.get_paginator("describe_cache_engine_versions")
    engines_versions = defaultdict(list)
    for engines_page in all_engines_paginator.paginate():  # noqa: WPS122
        for engine in engines_page["CacheEngineVersions"]:
            engines_versions[engine["Engine"]].append(engine["EngineVersion"])
    return dict(engines_versions)


@lru_cache
def parameter_group_family(engine: Text, engine_version: Text) -> Text:
    """Return the valid parameter group family for the specified cache engine and version.

    :param engine: Name of the cache engine (e.g. redis or memcached)
    :type engine: Text

    :param engine_version: Version of the cache engine being used (e.g. 3.1)
    :type engine_version: Text

    :returns: The name of the parameter group family for the specified engine and version

    :rtype: Text
    """
    engine_details = cache_client.describe_cache_engine_versions(
        Engine=engine, EngineVersion=engine_version
    )
    return engine_details["CacheEngineVersions"][0]["CacheParameterGroupFamily"]
