from functools import lru_cache
from typing import Literal

from pulumi import Output, ResourceOptions, StackReference


@lru_cache
def get_monitoring_sns_arn(level: Literal["warning", "critical"]) -> Output[str]:
    # This ends up creating a StackReference resource via a component resource which is
    # not great and can lead to conflicts / duplicate resources.
    #
    # We work around this by naming this stack resource `implicit` because it is a
    # side-effect of finding the sns_topic_arn for a given environment. This way
    # we can continue to explicitly create a StackReference("infrastructure.monitoring")
    # when we need it without getting duplicate resources.
    return StackReference(
        "implicit.infrastructure.monitoring",
        stack_name="infrastructure.monitoring",
        opts=ResourceOptions(delete_before_replace=True),
    ).require_output("opsgenie_sns_topics")[f"{level}_sns_topic_arn"]
