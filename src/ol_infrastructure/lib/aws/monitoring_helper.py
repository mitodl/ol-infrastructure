from typing import Literal
from pulumi import Output, StackReference
from functools import lru_cache


@lru_cache
def get_monitoring_sns_arn(level: Literal["warning", "critical"]) -> Output[str]:
    return StackReference("infrastructure.monitoring").require_output(
        "opsgenie_sns_topics"
    )[f"{level}_sns_topic_arn"]
