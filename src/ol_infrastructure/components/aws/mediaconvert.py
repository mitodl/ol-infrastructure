"""Resource Component for AWS MediaConvert"""

import json
from typing import Any, Optional

from pulumi import ComponentResource, ResourceOptions
from pulumi_aws import cloudwatch, iam, mediaconvert, sns
from pydantic import BaseModel, Field

from ol_infrastructure.lib.pulumi_helper import StackInfo


class MediaConvertConfig(BaseModel):
    """Configuration for AWS MediaConvert resources"""

    service_name: Optional[str] = Field(
        None,
        description="Name of the service using MediaConvert (e.g., 'ocw-studio')",
    )
    stack_info: StackInfo = Field(
        ..., description="Stack information including environment details"
    )
    aws_config: dict[str, Any] = Field(..., description="AWS configuration object")
    policy_arn: str = Field(
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
        stack_info = config.stack_info
        aws_config = config.aws_config
        policy_arn = config.policy_arn
        service_name = config.service_name
        host = config.host

        # Create resource prefix
        resource_prefix = (
            f"{service_name}-{stack_info.env_suffix}"
            if service_name
            else stack_info.env_suffix
        )

        super().__init__(
            "ol:infrastructure:aws:MediaConvert",
            f"{resource_prefix}-mediaconvert",
            None,
            opts,
        )

        # Create resource names
        queue_name = f"{resource_prefix}-mediaconvert-queue"
        role_name = f"{resource_prefix}-mediaconvert-role"
        topic_name = f"{resource_prefix}-mediaconvert-topic"
        policy_name = f"{resource_prefix}-mediaconvert-role-policy"
        subscription_name = f"{resource_prefix}-mediaconvert-topic-subscription"
        event_rule_name = f"{resource_prefix}-mediaconvert-cloudwatch-eventrule"
        event_target_name = f"{resource_prefix}-mediaconvert-cloudwatch-eventtarget"

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
                tags=aws_config["tags"],
                opts=ResourceOptions(parent=self, protect=False),
            )

        # Check if role exists
        try:
            existing_role = iam.get_role(name=role_name)
            self.role = iam.Role.get(
                role_name, existing_role.id, opts=ResourceOptions(parent=self)
            )
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
                tags=aws_config["tags"],
                opts=ResourceOptions(parent=self, protect=False),
            )

            # Attach policy to the role
            self.role_policy_attachment = iam.RolePolicyAttachment(
                policy_name,
                policy_arn=policy_arn,
                role=self.role.name,
                opts=ResourceOptions(parent=self, protect=False),
            )

        # Check if SNS topic exists
        try:
            existing_topic = sns.get_topic(name=topic_name)
            self.sns_topic = sns.Topic.get(
                topic_name, existing_topic.id, opts=ResourceOptions(parent=self)
            )
        except Exception:  # noqa: BLE001
            # Create SNS Topic for MediaConvert notifications if it doesn't exist
            self.sns_topic = sns.Topic(
                topic_name,
                name=topic_name,
                tags=aws_config["tags"],
                opts=ResourceOptions(parent=self, protect=False),
            )

            # Configure SNS Topic Subscription with provided host
            # (Subscription is idempotent so no need to check existence)
            self.sns_topic_subscription = sns.TopicSubscription(
                subscription_name,
                endpoint=f"https://{host}/api/transcode-jobs/",
                protocol="https",
                raw_message_delivery=True,
                topic=self.sns_topic.arn,
                opts=ResourceOptions(parent=self, protect=False),
            )

        # Check if CloudWatch EventRule exists
        try:
            existing_rule = cloudwatch.get_event_rule(name=event_rule_name)
            self.mediaconvert_cloudwatch_rule = cloudwatch.EventRule.get(
                event_rule_name, existing_rule.id
            )
        except Exception:  # noqa: BLE001
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
                opts=ResourceOptions(protect=False),
            )

        # EventTarget is idempotent - it will update if exists or create if not
        self.mediaconvert_cloudwatch_target = cloudwatch.EventTarget(
            event_target_name,
            rule=self.mediaconvert_cloudwatch_rule.name,
            arn=self.sns_topic.arn,
            opts=ResourceOptions(protect=False),
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
        env_suffix: str, account_id: str, service_name: str = ""
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

        resource_prefix = f"{service_name}-{env_suffix}" if service_name else env_suffix

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
                "Resource": (
                    f"arn:aws:iam::{account_id}:role/"
                    f"{resource_prefix}-mediaconvert-service-role"
                ),
            },
        ]
