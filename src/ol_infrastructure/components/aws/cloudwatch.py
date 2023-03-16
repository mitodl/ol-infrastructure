from typing import Optional

from pydantic import PositiveInt, validator
from pulumi import StackReference, ComponentResource, ResourceOptions
from ol_infrastructure.lib.ol_types import AWSBase

from pulumi_aws import CloudWatch


# "simple"
class OLCloudWatchAlarmSimpleConfig(AWSBase):
    """Abstract parent class of representing the configuration of
    an OL specific cloudwatch alarm.
    """

    comparison_operator: str
    datapoints_to_alarm: PositiveInt = PositiveInt(1)
    description: str
    enabled: bool = True
    evaluation_periods: PositiveInt = PositiveInt(2)
    level: str
    metric_name: str
    name: str
    namespace: str
    period: PositiveInt = PositiveInt(300)  # Five minutes
    statistic: str = "Average"
    threshold: int
    treat_missing_data_as: str = "missing"
    monitoring_stack: StackReference = StackReference(
        "infrastructure.monitoring.Production"
    )

    # TODO: MD 20230315 refactor to accomodate anomaly detection alerts
    @validator("comparison_operator")
    def is_valid_comparison_operator(
        cls: "OLCloudWatchAlarmSimpleConfig", comparison_operator: str  # noqa: N805
    ) -> str:
        valid_basic_operators = [
            "GreaterThanOrEqualToThreshold",
            "GreaterThanThreshold",
            "LessThanThreshold",
            "LessThanOrEqualToThreshold",
        ]
        if comparison_operator not in valid_basic_operators:
            raise ValueError(
                f"comparison_operator: {comparison_operator} is not valid. "
                "Valid operators are: {valid_basic_operators}"
            )
        return comparison_operator

    @validator("level")
    def is_valid_level(
        cls: "OLCloudWatchAlarmSimpleConfig", level: str  # noqa: N805
    ) -> str:
        if level.lower() not in ["warning", "critical"]:
            raise ValueError(
                f"level: {level} is not valid. Valid levels are "
                "'warning' and 'critical'"
            )
        return level.lower()

    @validator("treat_missing_data_as")
    def is_valid_missing_data_as(
        cls: "OLCloudWatchAlarmSimpleConfig", treat_missing_data_as: str  # noqa: N805
    ) -> str:
        valid_treat_missing_data_as = ["missing", "ignore", "breaching", "notBreaching"]
        if treat_missing_data_as not in valid_treat_missing_data_as:
            raise ValueError(
                f"treat_missing_data_as: {treat_missing_data_as} is not valid. "
                "Valid settings are: {valid_treat_missing_data_as}"
            )
        return treat_missing_data_as

    @validator("statistic")
    def is_valid_statistic(
        cls: "OLCloudWatchAlarmSimpleConfig", statistic: str  # noqa: N805
    ) -> str:
        valid_statistics = ["SampleCount", "Average", "Sum", "Minimum", "Maximum"]
        if statistic not in valid_statistics:
            raise ValueError(
                f"statistic: {statistic} is not valid. Valid statistics "
                "are: {valid_statistics}"
            )
        return statistic


class OLCloudWatchAlarmSimpleRDSConfig(OLCloudWatchAlarmSimpleConfig):
    """Configuration object for defining monitoring of an RDS deployment."""

    database_identifier: str
    namespace: str = "AWS/RDS"


class OLCloudWatchAlarmSimple(ComponentResource):
    """Abstract parent class of representing an OL specific cloudwatch alarm."""

    def __init__(
        self,
        alarm_config: OLCloudWatchAlarmSimpleRDSConfig,
        opts: Optional[ResourceOptions] = None,
    ):
        super().__init__(
            "ol:infrastructure.aws.cloudwatch.OLCloudWatchAlarmRDS",
            alarm_config.name,
            None,
            opts,
        )
        ResourceOptions(parent=self).merge(opts)  # type: ignore

        # Default alarm level to 'warning' if we some how get here with an invalid level
        if alarm_config.level == "critical":
            self.sns_topic_arn = self.monitoring_stack.get_output(
                "opsgenie_sns_topics"
            )["critical_sns_topic_arn"]
        else:
            self.sns_topic_arn = self.monitoring_stack.get_output(
                "opsgenie_sns_topics"
            )["warning_sns_topic_arn"]


class OLCloudWatchAlarmSimpleRDS(ComponentResource):
    metric_alarm: CloudWatch.MetricAlarm = None

    def __init__(
        self,
        alarm_config: OLCloudWatchAlarmSimpleRDSConfig,
        opts: Optional[ResourceOptions] = None,
    ):
        super().__init__(
            "ol:infrastructure.aws.cloudwatch.OLCloudWatchAlarmRDS",
            alarm_config.name,
            None,
            opts,
        )
        resource_options = ResourceOptions(parent=self).merge(opts)  # type: ignore

        self.metric_alarm = CloudWatch.MetricAlarm(
            f"simple-rds-alarm-{alarm_config.metric_name}-{alarm_config.database_identifier}",
            actions_enabled=True,
            alarm_actions=[self.sns_topic_arn],
            alarm_description=alarm_config.description,
            comparison_operator=alarm_config.comparison_operator,
            data_points_to_alarm=alarm_config.data_points_to_alarm,
            dimensions={
                "Name": "DBInstanceIdentifier",
                "Value": alarm_config.dimension_value,
            },
            evaluation_periods=alarm_config.evaluation_periods,
            metric_name=alarm_config.metric_name,
            name=f"simple-rds-alarm-{alarm_config.metric_name}-{alarm_config.database_identifier}",
            namespace=alarm_config.namespace,
            period=alarm_config.period,
            statistic=alarm_config.statistic,
            treat_missing_data_as=alarm_config.treat_missing_data_as,
            thresold=alarm_config.threshold,
            opts=resource_options,
        )
