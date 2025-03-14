"""Resource Component for AWS MediaConvert"""

import json
from typing import Any, Optional

from pulumi import ComponentResource, Output, ResourceOptions
from pulumi_aws import cloudwatch, iam, mediaconvert, sns
from pydantic import BaseModel, ConfigDict, Field

ROLE_NAME = "{resource_prefix}-role"


class MediaConvertConfig(BaseModel):
    """Configuration for AWS MediaConvert resources"""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    service_name: str = Field(
        description="Name of the service using MediaConvert (e.g., 'ocw-studio')",
    )
    env_suffix: str = Field(
        ..., description="Environment suffix for the MediaConvert resources"
    )
    tags: dict[str, str] = Field(..., description="Tags for the AWS resources")
    policy_arn: str | Output[str] = Field(
        ..., description="ARN of the IAM policy to attach to the MediaConvert role"
    )
    host: str = Field(..., description="Host for SNS notification subscriptions")


class OLMediaConvert(ComponentResource):
    """
    AWS MediaConvert component resource that creates:
    - MediaConvert Queue
    - MediaConvert Role
    - SNS Topic for notifications
    """

    def __init__(
        self,
        config: MediaConvertConfig,
        opts: Optional[ResourceOptions] = None,
    ):
        """Create an instance of the OLMediaConvert component resource"""

        # Extract values from config
        env_suffix = config.env_suffix
        tags = config.tags
        policy_arn = config.policy_arn
        service_name = config.service_name
        host = config.host

        # Create resource prefix
        resource_prefix = f"{service_name}-{env_suffix}-mediaconvert"

        super().__init__(
            "ol:infrastructure:aws:MediaConvert",
            resource_prefix,
            None,
            opts,
        )

        component_ops = ResourceOptions(parent=self).merge(opts)

        # Create resource names
        queue_name = f"{resource_prefix}-queue"
        role_name = ROLE_NAME.format(resource_prefix=resource_prefix)
        topic_name = f"{resource_prefix}-topic"
        policy_name = f"{resource_prefix}-role-policy"
        subscription_name = f"{resource_prefix}-topic-subscription"
        event_rule_name = f"{resource_prefix}-cloudwatch-eventrule"
        event_target_name = f"{resource_prefix}-cloudwatch-eventtarget"

        # Check if queue exists
        try:
            existing_queue = mediaconvert.get_queue(id=queue_name)
            self.queue = mediaconvert.Queue.get(
                queue_name, existing_queue.id, opts=ResourceOptions(parent=self)
            )
        except Exception:  # noqa: BLE001
            # Create MediaConvert Queue if it doesn't exist
            self.queue = mediaconvert.Queue(
                queue_name,
                description=f"{resource_prefix} MediaConvert Queue",
                name=queue_name,
                tags=tags,
                opts=component_ops,
            )

        # Check if role exists
        try:
            existing_role = iam.get_role(name=role_name)
            self.role = iam.Role.get(role_name, existing_role.id)
        except Exception:  # noqa: BLE001
            # Create MediaConvert Role if it doesn't exist
            self.role = iam.Role(
                role_name,
                assume_role_policy=json.dumps(
                    {
                        "Version": "2012-10-17",
                        "Statement": {
                            "Effect": "Allow",
                            "Action": "sts:AssumeRole",
                            "Principal": {"Service": "mediaconvert.amazonaws.com"},
                        },
                    }
                ),
                name=role_name,
                tags=tags,
                opts=component_ops,
            )

        # Attach policy to the role
        self.role_policy_attachment = iam.RolePolicyAttachment(
            policy_name,
            policy_arn=policy_arn,
            role=self.role.name,
            opts=component_ops,
        )

        # Check if SNS topic exists
        try:
            existing_topic = sns.get_topic(name=topic_name)
            self.sns_topic = sns.Topic.get(
                topic_name, existing_topic.id, opts=component_ops
            )
        except Exception:  # noqa: BLE001
            # Create SNS Topic for MediaConvert notifications if it doesn't exist
            self.sns_topic = sns.Topic(
                topic_name,
                name=topic_name,
                tags=tags,
                opts=component_ops,
            )

        # Configure SNS Topic Subscription with provided host
        # (Subscription is idempotent so no need to check existence)
        self.sns_topic_subscription = sns.TopicSubscription(
            subscription_name,
            endpoint=f"https://{host}/api/transcode-jobs/",
            protocol="https",
            raw_message_delivery=True,
            topic=self.sns_topic.arn,
            opts=component_ops,
        )

        # Check if CloudWatch EventRule exists
        try:
            # Configure Cloudwatch EventRule if it doesn't exist
            self.mediaconvert_cloudwatch_rule = cloudwatch.EventRule(
                event_rule_name,
                description="Capture MediaConvert Events for use with SNS",
                name=event_rule_name,
                event_pattern=json.dumps(
                    {
                        "source": ["aws.mediaconvert"],
                        "detail-type": ["MediaConvert Job State Change"],
                        "detail": {
                            "userMetadata": {"filter": [queue_name]},
                            "status": ["COMPLETE", "ERROR"],
                        },
                    }
                ),
                opts=component_ops,
            )

            # EventTarget is idempotent - it will update if exists or create if not
            self.mediaconvert_cloudwatch_target = cloudwatch.EventTarget(
                event_target_name,
                rule=self.mediaconvert_cloudwatch_rule.name,
                arn=self.sns_topic.arn,
                opts=component_ops,
            )

        except Exception:  # noqa: BLE001, S110
            pass

        # Register outputs
        outputs = {
            "queue": self.queue,
            "role": self.role,
            "sns_topic": self.sns_topic,
            "role_policy_attachment": self.role_policy_attachment,
            "sns_topic_subscription": self.sns_topic_subscription,
        }

        self.register_outputs(outputs)

    @staticmethod
    def get_standard_policy_statements(
        env_suffix: str, service_name: str
    ) -> list[dict[str, Any]]:
        """Return a standardized set of IAM policy statements for MediaConvert access.

        Args:
            stack_info: Stack information including environment details
            account_id: AWS account ID where the role exists
            service_name: Name of the service using MediaConvert
                (e.g., 'ovs', 'ocw-studio')

        Returns:
            List of IAM policy statements for MediaConvert access
        """

        resource_prefix = f"{service_name}-{env_suffix}"

        return [
            {
                "Effect": "Allow",
                "Action": [
                    "mediaconvert:AssociateCertificate",
                    "mediaconvert:CreateJob",
                    "mediaconvert:CreatePreset",
                    "mediaconvert:CreateQueue",
                    "mediaconvert:DescribeEndpoints",
                    "mediaconvert:DisassociateCertificate",
                    "mediaconvert:ListJobTemplates",
                    "mediaconvert:ListPresets",
                    "mediaconvert:ListQueues",
                ],
                "Resource": "*",
            },
            {
                "Effect": "Allow",
                "Action": "mediaconvert:GetJob",
                "Resource": "arn:*:mediaconvert:*:*:jobs/*",
            },
            {
                "Effect": "Allow",
                "Action": "iam:PassRole",
                "Resource": (
                    f"arn:aws:iam::*:role/{ROLE_NAME.format(resource_prefix=resource_prefix)}"
                ),
            },
        ]
