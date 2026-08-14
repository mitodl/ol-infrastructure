from collections import defaultdict
from enum import StrEnum, unique
from functools import lru_cache

import boto3
import pulumi
from botocore.exceptions import ClientError

rds_client = boto3.client("rds")
ec2_client = boto3.client("ec2")

# The RDS default parameter groups for PostgreSQL set
# ``max_connections = LEAST({DBInstanceClassMemory/9531392}, 5000)``. These two
# constants are that formula.
POSTGRES_BYTES_PER_CONNECTION = 9531392
POSTGRES_MAX_CONNECTIONS_CAP = 5000


@unique
class DBInstanceTypes(StrEnum):
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


@lru_cache
def postgres_max_connections(db_instance_type: str) -> int:
    """Resolve the effective ``max_connections`` for a PostgreSQL RDS instance class.

    RDS's default parameter group computes this as
    ``LEAST({DBInstanceClassMemory/9531392}, 5000)``, so it varies with the instance
    class. Anything sizing a connection pool against the database needs the real
    number rather than the 5000 cap, which only the larger classes actually reach --
    a ``db.m7g.large`` tops out around 900.

    **This is an upper bound, not an exact figure.** ``DBInstanceClassMemory`` is the
    memory RDS leaves to the database after its own OS and management reservations,
    which is less than the instance class's total physical memory used here, and AWS
    does not publish the reservation.

    Measured on both sides of the cap:

    - ``db.r7g.2xlarge`` (``ol-etl-db-production``) -- computes 7210, clamped to 5000,
      and ``SHOW max_connections`` returns 5000. **Exact**, because the cap binds.
    - ``db.m7g.large`` (``ol-etl-db-qa``) -- computes 901, but ``SHOW max_connections``
      returns **832**. RDS withholds ~629 MiB of the 8 GiB, so this **overstates by
      ~8%**.

    Below the cap, then, callers must leave at least ~10% headroom on top of whatever
    margin they want for reserved and administrative connections -- budgeting straight
    to this number would overcommit.

    :param db_instance_type: An RDS instance class, e.g. ``db.r7g.2xlarge``

    :returns: An upper bound on the connections the instance will accept; exact for
        classes that reach the 5000 cap

    :rtype: int
    """
    # RDS instance classes are the EC2 class with a ``db.`` prefix, and the
    # DescribeDBInstance APIs don't report instance memory, so resolve it from EC2.
    try:
        instance_types = ec2_client.describe_instance_types(
            InstanceTypes=[db_instance_type.removeprefix("db.")]
        )["InstanceTypes"]
    except ClientError as exc:
        # An unknown type raises InvalidInstanceType rather than returning an empty
        # list. The instance class comes from Pulumi config, so a typo lands here, and
        # AWS reports the stripped EC2 name -- report the value as configured instead.
        msg = f"No EC2 instance type matching RDS instance class {db_instance_type}"
        raise ValueError(msg) from exc
    memory_bytes = instance_types[0]["MemoryInfo"]["SizeInMiB"] * 1024 * 1024
    return min(
        memory_bytes // POSTGRES_BYTES_PER_CONNECTION, POSTGRES_MAX_CONNECTIONS_CAP
    )


def engine_major_version(engine_version: str) -> str:
    return engine_version.split(".", maxsplit=1)[0]


def is_minor_version_change(current_version: str, desired_version: str) -> bool:
    """Determine if the version change is only a minor/patch version change.

    A minor version change is when only the patch/minor version differs,
    but the major version remains the same.

    :param current_version: The current engine version (e.g., "18.1")
    :param desired_version: The desired engine version (e.g., "18.2")

    :returns: True if the change is only a minor/patch version change, False otherwise

    :rtype: bool
    """
    return engine_major_version(current_version) == engine_major_version(
        desired_version
    )


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


def turn_off_deletion_protection(
    db_identifier: str, *, currently_protected: bool = True
):
    """Disable deletion protection for the specified RDS database instance.

    ModifyDBInstance is a live, privileged write against the target database, so it is
    skipped during a `pulumi preview` and when protection is already off. A preview that
    performs this call fails outright wherever the worker lacks rds:ModifyDBInstance,
    and under the preview-gated Concourse topology a failed preview means the promotion
    gate is never opened and the environment cannot be deployed at all.

    :param db_identifier: The identifier of the RDS database instance.
    :type db_identifier: str

    :param currently_protected: Whether the live instance currently has deletion
        protection enabled, as reported by DescribeDBInstances.
    :type currently_protected: bool

    :raises botocore.exceptions.ClientError: If the AWS API call fails or the instance
    does not exist.
    """
    if pulumi.runtime.is_dry_run() or not currently_protected:
        return
    try:
        rds_client.modify_db_instance(
            DBInstanceIdentifier=db_identifier,
            ApplyImmediately=True,
            DeletionProtection=False,
        )
    except rds_client.exceptions.DBInstanceNotFoundFault as e:
        msg = f"DB instance '{db_identifier}' not found."
        raise ValueError(msg) from e
    except rds_client.exceptions.InvalidDBInstanceStateFault as e:
        msg = f"DB instance '{db_identifier}' is in an invalid state for modification."
        raise RuntimeError(msg) from e
