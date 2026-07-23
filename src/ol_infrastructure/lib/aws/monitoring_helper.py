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
    monitoring_stack = StackReference(
        "implicit.infrastructure.monitoring",
        stack_name=stack_ref(projects.MONITORING, "default"),
    )
    # Downstream stacks redeploy independently, on their own schedule, so this
    # code can reach a stack's deploy before the monitoring stack itself has
    # been applied with the notification_sns_topics export (added alongside
    # the legacy opsgenie_sns_topics one -- see monitoring/__main__.py).
    # get_output returns None for a missing key instead of raising, for both
    # exports, so a stack picking up this code before the monitoring stack's
    # own redeploy still resolves via the legacy export rather than hard
    # failing. If the monitoring stack has neither export -- which shouldn't
    # happen today (the legacy export is already live), but would if a
    # future change ever dropped it before this one -- raise a clear error
    # instead of a bare TypeError from indexing into None.
    new_topics = monitoring_stack.get_output("notification_sns_topics")
    legacy_topics = monitoring_stack.get_output("opsgenie_sns_topics")

    def _resolve_topic_arn(topics: list[dict[str, str] | None]) -> str:
        resolved = topics[0] or topics[1]
        if resolved is None:
            msg = (
                "Neither the notification_sns_topics nor the legacy "
                "opsgenie_sns_topics export was found on the monitoring "
                "stack -- has it been deployed at least once?"
            )
            raise ValueError(msg)
        return resolved[f"{level}_sns_topic_arn"]

    return Output.all(new_topics, legacy_topics).apply(_resolve_topic_arn)
