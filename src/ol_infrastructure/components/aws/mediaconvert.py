"""Resource Component for AWS MediaConvert"""

import json
from typing import Any, Optional

from pulumi import ComponentResource, ResourceOptions
from pulumi_aws import cloudwatch, iam, mediaconvert, sns
from pydantic import BaseModel, Field


class MediaConvertConfig(BaseModel):
    """Configuration for AWS MediaConvert resources"""

    service_name: str = Field(
        ...,
        description="Name of the service using MediaConvert (e.g., 'ocw-studio')",
    )
    stack_info: dict = Field(
        ..., description="Stack information including environment details"
    )
    aws_config: dict = Field(..., description="AWS configuration object")
    policy_arn: str = Field(
        ..., description="ARN of the IAM policy to attach to the MediaConvert role"
    )
    host: Optional[str] = Field(
        None, description="Host for SNS notification subscriptions"
    )
    custom_queue_name: Optional[str] = Field(
        None, description="Optional custom name for the MediaConvert queue"
    )
    custom_role_name: Optional[str] = Field(
        None, description="Optional custom name for the MediaConvert role"
    )
    custom_topic_name: Optional[str] = Field(
        None, description="Optional custom name for the SNS topic"
    )


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

        super().__init__(
            "ol:infrastructure:aws:MediaConvert",
            f"{config.service_name}-{config.stack_info.env_suffix}-mediaconvert",
            None,
            opts,
        )

        # Extract values from config
        stack_info = config.stack_info
        aws_config = config.aws_config
        policy_arn = config.policy_arn
        service_name = config.service_name
        host = config.host

        # Create resource prefix
        resource_prefix = f"{service_name}-{stack_info.env_suffix}"

        # Create resource names
        queue_name = config.custom_queue_name or f"{resource_prefix}-mediaconvert-queue"
        role_name = (
            config.custom_role_name or f"{resource_prefix}-mediaconvert-service-role"
        )
        topic_name = config.custom_topic_name or f"{resource_prefix}-mediaconvert"

        # Create MediaConvert Queue
        self.queue = mediaconvert.Queue(
            f"{resource_prefix}-mediaconvert-queue",
            description=f"{resource_prefix} MediaConvert Queue",
            name=queue_name,
            tags=aws_config.tags,
            opts=ResourceOptions(parent=self),
        )

        # Create MediaConvert Role
        self.role = iam.Role(
            f"{resource_prefix}-mediaconvert-role",
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
            tags=aws_config.tags,
            opts=ResourceOptions(parent=self),
        )

        # Attach policy to the role
        self.role_policy_attachment = iam.RolePolicyAttachment(
            f"{resource_prefix}-mediaconvert-role-policy",
            policy_arn=policy_arn,
            role=self.role.name,
            opts=ResourceOptions(parent=self),
        )

        # Create SNS Topic for MediaConvert notifications
        self.sns_topic = sns.Topic(
            f"{resource_prefix}-mediaconvert-topic",
            name=topic_name,
            tags=aws_config.tags,
            opts=ResourceOptions(parent=self),
        )

        # Configure SNS Topic Subscription with provided host
        self.sns_topic_subscription = sns.TopicSubscription(
            f"{resource_prefix}-sns-topic-subscription",
            endpoint=f"https://{host}/api/transcode-jobs/",
            protocol="https",
            raw_message_delivery=True,
            topic=self.sns_topic.arn,
            opts=ResourceOptions(parent=self),
        )

        # Configure Cloudwatch EventRule and EventTarget
        self.mediaconvert_cloudwatch_rule = cloudwatch.EventRule(
            f"{resource_prefix}-mediaconvert-cloudwatch-eventrule",
            description="Capture MediaConvert Events for use with SNS",
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
        )

        self.mediaconvert_cloudwatch_target = cloudwatch.EventTarget(
            f"{resource_prefix}-mediaconvert-cloudwatch-eventtarget",
            rule=self.mediaconvert_cloudwatch_rule.name,
            arn=self.sns_topic.arn,
        )

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
        stack_info: dict, account_id: str, service_name: str
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

        resource_prefix = f"{service_name}-{stack_info.env_suffix}"

        return [
            {
                "Effect": "Allow",
                "Action": [
                    "mediaconvert:ListQueues",
                    "mediaconvert:DescribeEndpoints",
                    "mediaconvert:ListPresets",
                    "mediaconvert:CreatePreset",
                    "mediaconvert:DisassociateCertificate",
                    "mediaconvert:CreateQueue",
                    "mediaconvert:AssociateCertificate",
                    "mediaconvert:CreateJob",
                    "mediaconvert:ListJobTemplates",
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
                "Resource": (f"arn:aws:iam::{account_id}:role/"
                f"{resource_prefix}-mediaconvert-service-role"),
            },
        ]
