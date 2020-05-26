# coding: utf-8

from collections import defaultdict
from typing import Dict, List, Text

import boto3

rds_client = boto3.client('rds')


def db_engines() -> Dict[Text, List[Text]]:
    """Generate a list of database engines and their currently available versions on RDS.

    :returns: Dictionary of engine names and the list of available versions
    :rtype: Dict[Text, List[Text]]

    """
    all_engines_paginator = rds_client.get_paginator('describe_db_engine_versions')
    engines_versions = defaultdict(list)
    for engines_page in all_engines_paginator.paginate(): # noqa: WPS122
        for engine in engines_page['DBEngineVersions']:
            engines_versions[engine['Engine']].append(engine['EngineVersion'])
    return dict(engines_versions)


def parameter_group_family(engine: Text, engine_version: Text) -> Text:
    """Return the valid parameter group family for the specified DB engine and version.

    :param engine: Name of the RDS database engine (e.g. postgres, mysql, etc.)
    :type engine: Text

    :param engine_version: Version of the RDS database engine being used (e.g. 12.2)
    :type engine_version: Text

    :returns: The name of the parameter group family for the specified engine and version

    :rtype: Text
    """
    engine_details = rds_client.describe_db_engine_versions(Engine=engine, EngineVersion=engine_version)
    return engine_details['DBParameterGroupFamily']
