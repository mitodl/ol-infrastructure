from typing import Any

from ol_infrastructure.components.aws.database import OLReplicaDBConfig
from ol_infrastructure.lib.aws.cache_helper import CacheInstanceTypes
from ol_infrastructure.lib.aws.opensearch_helper import SearchInstanceTypes
from ol_infrastructure.lib.aws.rds_helper import DBInstanceTypes
from ol_infrastructure.lib.pulumi_helper import StackInfo

production_defaults = {
    "rds": {
        "multi_az": True,
        "instance_size": DBInstanceTypes.general_purpose_large.value,
        "read_replica": OLReplicaDBConfig(),
        "monitoring_profile_name": "production",
    },
    "redis": {"instance_type": CacheInstanceTypes.high_mem_large},
    "opensearch": {
        "instance_type": SearchInstanceTypes.high_mem_regular.value,
        "instance_count": 3,
    },
}

qa_defaults = {
    "rds": {
        "instance_size": DBInstanceTypes.medium.value,
        "multi_az": False,
        "prevent_delete": False,
        "take_final_snapshot": False,
        "backup_days": 7,
        "monitoring_profile_name": "qa",
    },
    "redis": {"instance_type": CacheInstanceTypes.small},
    "opensearch": {
        "instance_type": SearchInstanceTypes.medium.value,
        "instance_count": 3,
    },
}

ci_defaults = {
    "rds": {
        "instance_size": DBInstanceTypes.small.value,
        "multi_az": False,
        "prevent_delete": False,
        "take_final_snapshot": False,
        "backup_days": 1,
        "monitoring_profile_name": "ci",
    },
    "redis": {"instance_type": CacheInstanceTypes.small},
    "opensearch": {
        "instance_type": SearchInstanceTypes.medium.value,
        "instance_count": 3,
    },
}


env_dict = {"ci": ci_defaults, "qa": qa_defaults, "production": production_defaults}


def defaults(stack_info: StackInfo) -> dict[str, Any]:
    """Provide a single location to dispatch infrastructure defaults based on env.

    :param stack_info: The stack_info object that has been parsed from the Pulumi stack.
    :type stack_info: StackInfo

    :returns: A dictionary containing the default parameters for a given environment.

    :rtype: Dict[Text, Any]
    """
    return env_dict[stack_info.env_suffix]
