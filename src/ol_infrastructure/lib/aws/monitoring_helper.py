from functools import lru_cache
from typing import Literal

from pulumi import Output, StackReference

from ol_infrastructure.lib import pulumi_projects as projects
from ol_infrastructure.lib.pulumi_helper import stack_ref


@lru_cache
def get_monitoring_sns_arn(level: Literal["warning", "critical"]) -> Output[str]:
    # This ends up creating a StackReference resource via a component resource which is
    # not great and can lead to conflicts / duplicate resources.
    #
    # We work around this by naming this stack resource `implicit` because it is a
    # side-effect of finding the sns_topic_arn for a given environment. This way
    # we can continue to explicitly create a StackReference to monitoring
    # when we need it without getting duplicate resources.
    #
    # delete_before_replace is intentionally omitted (defaults to False) so that
    # if the referenced stack name changes Pulumi creates the new reference before
    # removing the old one, avoiding a failed read from the now-gone legacy stack.
    return StackReference(
        "implicit.infrastructure.monitoring",
        stack_name=stack_ref(projects.MONITORING, "default"),
    ).require_output("notification_sns_topics")[f"{level}_sns_topic_arn"]
