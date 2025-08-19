from collections import defaultdict
from enum import Enum, unique
from functools import lru_cache

import boto3

rds_client = boto3.client("rds")


@unique
class DBInstanceTypes(str, Enum):
    small = "db.t4g.small"
    medium = "db.t4g.medium"
    large = "db.t4g.large"
    xlarge = "db.t4g.xlarge"
    general_purpose_large = "db.m7g.large"
    general_purpose_xlarge = "db.m7g.xlarge"
    high_mem_regular = "db.r7g.large"
    high_mem_xlarge = "db.r7g.xlarge"


@lru_cache
def db_engines() -> dict[str, list[str]]:
    """Generate a list of database engines and their currently available versions on
    RDS.

    :returns: Dictionary of engine names and the list of available versions

    :rtype: Dict[str, List[str]]
    """
    all_engines_paginator = rds_client.get_paginator("describe_db_engine_versions")
    engines_versions = defaultdict(list)
    for engines_page in all_engines_paginator.paginate():
        for engine in engines_page["DBEngineVersions"]:
            engines_versions[engine["Engine"]].append(engine["EngineVersion"])
    return dict(engines_versions)


def engine_major_version(engine_version: str) -> str:
    return engine_version.split(".", maxsplit=1)[0]


def max_minor_version(engine: str, major_version: int | str) -> str:
    """
    Given a database egine and the major version, determine the current maximum minor
    version.

    :param engine: The database engine being targeted
    :param major_version: The major version of the engine

    :returns: The full version string of the current highest minor version
    """
    versions = db_engines().get(engine)
    if not versions:
        msg = "The specified engine does not have any available versions"
        raise ValueError(msg)
    major_versions = defaultdict(list)
    for version in versions:
        major, minor_and_patch = version.rsplit(".", maxsplit=1)
        major_versions[major].append(minor_and_patch)
    highest_minor = sorted(major_versions[str(major_version)], key=int)[-1]
    return f"{major_version}.{highest_minor}"


@lru_cache
def parameter_group_family(engine: str, engine_version: str) -> str:
    """Return the valid parameter group family for the specified DB engine and version.

    :param engine: Name of the RDS database engine (e.g. postgres, mysql, etc.)
    :type engine: str

    :param engine_version: Version of the RDS database engine being used (e.g. 12.2)
    :type engine_version: str

    :returns: The name of the parameter group family for the specified
        engine and version.

    :rtype: str
    """
    engine_details = rds_client.describe_db_engine_versions(
        Engine=engine, EngineVersion=engine_version
    )
    return engine_details["DBEngineVersions"][0]["DBParameterGroupFamily"]


def get_rds_instance(instance_name: str) -> dict[str, str]:
    try:
        db_instances = rds_client.describe_db_instances(
            DBInstanceIdentifier=instance_name,
        )
        db_instances = db_instances.pop("DBInstances")
        if len(db_instances) > 1:
            msg = (
                "More than one database instance was found. "
                "Please provide a more specific instance name."
            )
            raise ValueError(msg)
        db_instance = db_instances[0]
    except rds_client.exceptions.DBInstanceNotFoundFault:
        db_instance = {}
    return db_instance


def turn_off_deletion_protection(db_identifier: str):
    rds_client.modify_db_instance(
        DBInstanceIdentifier=db_identifier,
        ApplyImmediately=True,
        DeletionProtection=False,
    try:
        rds_client.modify_db_instance(
            DBInstanceIdentifier=db_identifier,
            ApplyImmediately=True,
            DeletionProtection=False,
        )
    except rds_client.exceptions.DBInstanceNotFoundFault:
        raise ValueError(f"DB instance '{db_identifier}' not found.")
    except rds_client.exceptions.InvalidDBInstanceStateFault:
        raise RuntimeError(f"DB instance '{db_identifier}' is in an invalid state for modification.")
