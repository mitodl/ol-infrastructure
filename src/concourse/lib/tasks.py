from concourse.lib.constants import REGISTRY_IMAGE
from concourse.lib.models.pipeline import Command, Identifier, TaskConfig, TaskStep


# Generates a TaskStep to perform an instance refresh from a given set of filters and queires.
# The combination of filters and queries should be trusted to return one, and only one,
# autoscale group name.
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
                path="sh",
                args=[
                    "-exc",
                    f""" ASG_NAME=$(aws autoscaling describe-auto-scaling-groups --color on --no-cli-auto-prompt --no-cli-pager --filters {filters} --query "{queries}" --output text);
                    aws autoscaling start-instance-refresh --color on  --no-cli-auto-prompt --no-cli-pager --auto-scaling-group-name $ASG_NAME --preferences MinHealthyPercentage=50,InstanceWarmup=120""",  # noqa: WPS318
                ],
            ),
        ),
    )
