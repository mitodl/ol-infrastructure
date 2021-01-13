from typing import Any, Dict, Text

from ol_infrastructure.lib.pulumi_helper import StackInfo

production_defaults = {
    "rds": {"multi_az": True, "instance_size": "db.m6g.large"},
    "redis": {"instance_type": "cache.m6g.large"},
}

qa_defaults = {
    "rds": {
        "instance_size": "db.t3.medium",
        "multi_az": False,
        "prevent_delete": False,
        "take_final_snapshot": False,
        "backup_days": 7,
    },
    "redis": {"instance_type": "cache.t3.small"},
}

ci_defaults = {
    "rds": {
        "instance_size": "db.t3.medium",
        "multi_az": False,
        "prevent_delete": False,
        "take_final_snapshot": False,
        "backup_days": 1,
    },
    "redis": {"instance_type": "cache.t3.small"},
}


def defaults(stack_info: StackInfo) -> Dict[Text, Any]:
    """Provides a single location to dispatch infrastructure defaults based on env.

    :param stack_info: The stack_info object that has been parsed from the Pulumi stack.
    :type stack_info: StackInfo

    :returns: A dictionary containing the default parameters for a given environment.

    :rtype: Dict[Text, Any]
    """
    if stack_info.env_suffix == "qa":
        return qa_defaults
    return production_defaults
