from collections import defaultdict
from enum import Enum, unique
from functools import lru_cache

import boto3

rds_client = boto3.client("rds")


@unique
class DBInstanceTypes(str, Enum):  # noqa: WPS600
    small = "db.t3.small"
    medium = "db.t3.medium"
    large = "db.t3.large"
    general_purpose_large = "db.m6g.large"
    general_purpose_xlarge = "db.m6g.xlarge"
    high_mem_regular = "db.r6g.large"
    high_mem_xlarge = "db.r6g.xlarge"


@lru_cache
def db_engines() -> dict[str, list[str]]:
    """Generate a list of database engines and their currently available versions on RDS.

    :returns: Dictionary of engine names and the list of available versions
    :rtype: Dict[str, List[str]]
    """
    all_engines_paginator = rds_client.get_paginator("describe_db_engine_versions")
    engines_versions = defaultdict(list)
    for engines_page in all_engines_paginator.paginate():
        for engine in engines_page["DBEngineVersions"]:
            engines_versions[engine["Engine"]].append(engine["EngineVersion"])
    return dict(engines_versions)


@lru_cache
def parameter_group_family(engine: str, engine_version: str) -> str:
    """Return the valid parameter group family for the specified DB engine and version.

    :param engine: Name of the RDS database engine (e.g. postgres, mysql, etc.)
    :type engine: str

    :param engine_version: Version of the RDS database engine being used (e.g. 12.2)
    :type engine_version: str

    :returns: The name of the parameter group family for the specified engine and version  # noqa: E501

    :rtype: str
    """
    engine_details = rds_client.describe_db_engine_versions(
        Engine=engine, EngineVersion=engine_version
    )
    return engine_details["DBEngineVersions"][0]["DBParameterGroupFamily"]
