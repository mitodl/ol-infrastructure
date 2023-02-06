from concourse.lib.constants import REGISTRY_IMAGE
from concourse.lib.models.pipeline import Command, Identifier, TaskConfig, TaskStep


# Generates a TaskStep to perform an instance refresh from a given set
# of filters and queires. The combination of filters and queries should
# be trusted to return one, and only one, autoscale group name.
def instance_refresh_task(
    filters: str,
    queries: str,
) -> TaskStep:
    return TaskStep(
        task=Identifier("instance-refresh"),
        privileged=False,
        config=TaskConfig(
            platform="linux",
            image_resource={
                "type": REGISTRY_IMAGE,
                "source": {"repository": "amazon/aws-cli"},
            },
            params={},
            run=Command(
                path="bash",
                args=[
                    "-ec",
                    f"""ASG_NAME=$(aws autoscaling describe-auto-scaling-groups --color on --no-cli-auto-prompt --no-cli-pager --filters {filters} --query "{queries}" --output text);
                    aws autoscaling start-instance-refresh --color on --no-cli-auto-prompt --no-cli-pager --auto-scaling-group-name $ASG_NAME --preferences MinHealthyPercentage=50,InstanceWarmup=120""",  # noqa: #501
                ],
            ),
        ),
    )


# Generates a TaskStep that can be used to block a job from completing until
# the most recent instance refresh is completed. If no instance refresh is
# found, the task finishes immediately. The combination filters + queries is
# expected to return one and only one autoscale group name.
def block_for_instance_refresh_task(
    filters: str,
    queries: str,
    check_freq: int = 10,
) -> TaskStep:
    return TaskStep(
        task=Identifier("block-for-instance-refresh"),
        privileged=False,
        config=TaskConfig(
            platform="linux",
            image_resource={
                "type": REGISTRY_IMAGE,
                "source": {"repository": "amazon/aws-cli"},
            },
            params={},
            run=Command(
                path="bash",
                args=[
                    "-ec",
                    f""" ASG_NAME=$(aws autoscaling describe-auto-scaling-groups --color on --no-cli-auto-prompt --no-cli-pager --filters {filters} --query "{queries}" --output text);
                    status="InProgress"
                    while [ "$status" = "InProgress" ] || [ "$status" == "Pending" ] || [ "$status" == "Canceling" ]
                    do
                        sleep {check_freq}
                        status=$(aws autoscaling describe-instance-refreshes --color on --no-cli-auto-prompt --no-cli-pager --auto-scaling-group-name $ASG_NAME --query "sort_by(InstanceRefreshes, &StartTime)[].{{Status: Status}}" --output text | tail -n 1)
                        aws autoscaling describe-instance-refreshes --color on --no-cli-auto-prompt --no-cli-pager --auto-scaling-group-name $ASG_NAME --query "sort_by(InstanceRefreshes, &StartTime)[].{{InstanceRefreshId: InstanceRefreshId, StartTime: StartTime, Status: Status}}" --output text | tail -n 1
                    done""",  # noqa: #501
                ],
            ),
        ),
    )
