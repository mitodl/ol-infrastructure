from typing import Any

from ol_infrastructure.components.aws.database import OLReplicaDBConfig
from ol_infrastructure.lib.aws.rds_helper import DBInstanceTypes
from ol_infrastructure.lib.pulumi_helper import StackInfo

production_defaults = {
    "rds": {
        "multi_az": True,
        "instance_size": DBInstanceTypes.general_purpose_large.value,
        "read_replica": OLReplicaDBConfig(),
    },
    "redis": {"instance_type": "cache.m6g.large"},
}

qa_defaults = {
    "rds": {
        "instance_size": DBInstanceTypes.medium.value,
        "multi_az": False,
        "prevent_delete": False,
        "take_final_snapshot": False,
        "backup_days": 7,
    },
    "redis": {"instance_type": "cache.t3.small"},
}

ci_defaults = {
    "rds": {
        "instance_size": DBInstanceTypes.medium.value,
        "multi_az": False,
        "prevent_delete": False,
        "take_final_snapshot": False,
        "backup_days": 1,
    },
    "redis": {"instance_type": "cache.t3.small"},
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
