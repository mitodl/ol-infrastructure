"""Resource Component for AWS MediaConvert"""

import json
from typing import Any

from pulumi import ComponentResource, Output, ResourceOptions
from pulumi_aws import cloudwatch, get_caller_identity, iam, mediaconvert, sns
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
        opts: ResourceOptions | None = None,
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

        self.queue = mediaconvert.Queue(
            queue_name,
            description=f"{resource_prefix} MediaConvert Queue",
            name=queue_name,
            tags=tags,
            opts=component_ops,
        )

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

        self.sns_topic = sns.Topic(
            topic_name,
            name=topic_name,
            tags=tags,
            opts=component_ops,
        )

        aws_account = get_caller_identity()

        policy_document = self.sns_topic.arn.apply(
            lambda arn: iam.get_policy_document_output(
                statements=[
                    {
                        "Sid": "__default_statement_ID",
                        "Effect": "Allow",
                        "principals": [{"type": "AWS", "identifiers": ["*"]}],
                        "actions": [
                            "SNS:GetTopicAttributes",
                            "SNS:SetTopicAttributes",
                            "SNS:AddPermission",
                            "SNS:RemovePermission",
                            "SNS:DeleteTopic",
                            "SNS:Subscribe",
                            "SNS:ListSubscriptionsByTopic",
                            "SNS:Publish",
                        ],
                        "resources": [arn],
                        "conditions": [
                            iam.GetPolicyDocumentStatementConditionArgs(
                                test="StringEquals",
                                variable="AWS:SourceOwner",
                                values=[aws_account.account_id],
                            )
                        ],
                    },
                    {
                        "Sid": "AllowEventsServiceToPublish",
                        "Effect": "Allow",
                        "principals": [
                            {"type": "Service", "identifiers": ["events.amazonaws.com"]}
                        ],
                        "actions": ["SNS:Publish"],
                        "resources": [arn],
                    },
                ]
            )
        )
        self.sns_topic_policy = sns.TopicPolicy(
            f"{topic_name}-policy",
            arn=self.sns_topic.arn,
            policy=policy_document.json,
        )

        # Configure SNS Topic Subscription with provided host
        self.sns_topic_subscription = sns.TopicSubscription(
            subscription_name,
            endpoint=f"https://{host}/api/transcode-jobs/",
            protocol="https",
            raw_message_delivery=True,
            topic=self.sns_topic.arn,
            opts=component_ops,
        )

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
            tags=tags,
            opts=component_ops,
        )

        self.mediaconvert_cloudwatch_target = cloudwatch.EventTarget(
            event_target_name[:63],
            target_id=event_target_name[:63],
            rule=self.mediaconvert_cloudwatch_rule.name,
            arn=self.sns_topic.arn,
            opts=component_ops,
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
        env_suffix: str, service_name: str
    ) -> list[dict[str, Any]]:
        """Return a standardized set of IAM policy statements for MediaConvert access.

        Args:
            stack_info: Stack information including environment details
            service_name: Name of the service using MediaConvert
                (e.g., 'ovs', 'ocw-studio')

        Returns:
            List of IAM policy statements for MediaConvert access
        """

        resource_prefix = f"{service_name}-{env_suffix}-mediaconvert"

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
