from typing import Optional, Union


from pydantic import PositiveInt, validator, BaseModel
from pulumi import ComponentResource, ResourceOptions, Output
from ol_infrastructure.lib.aws.monitoring_helper import get_monitoring_sns_arn

from pulumi_aws import cloudwatch


# "simple"
class OLCloudWatchAlarmSimpleConfig(BaseModel):
    """Abstract parent class of representing the configuration of
    an OL specific cloudwatch alarm.
    """

    comparison_operator: str
    datapoints_to_alarm: PositiveInt = 1
    description: str
    enabled: bool = True
    evaluation_periods: PositiveInt = 2
    level: str = "warning"
    metric_name: str
    name: str
    namespace: str
    period: PositiveInt = PositiveInt(300)  # Five minutes
    statistic: str = "Average"
    threshold: int
    treat_missing_data_as: str = "missing"
    unit: Optional[str]

    # TODO: MD 20230315 refactor to accomodate anomaly detection alerts
    @validator("comparison_operator")
    def is_valid_comparison_operator(
        cls,  # noqa: N805
        comparison_operator: str,
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
    def is_valid_level(cls, level: str) -> str:  # noqa: N805
        if level.lower() not in ["warning", "critical"]:
            raise ValueError(
                f"level: {level} is not valid. Valid levels are "
                "'warning' and 'critical'"
            )
        return level.lower()

    @validator("treat_missing_data_as")
    def is_valid_missing_data_as(
        cls,  # noqa: N805
        treat_missing_data_as: str,
    ) -> str:
        valid_treat_missing_data_as = ["missing", "ignore", "breaching", "notBreaching"]
        if treat_missing_data_as not in valid_treat_missing_data_as:
            raise ValueError(
                f"treat_missing_data_as: {treat_missing_data_as} is not valid. "
                "Valid settings are: {valid_treat_missing_data_as}"
            )
        return treat_missing_data_as

    @validator("statistic")
    def is_valid_statistic(cls, statistic: str) -> str:  # noqa: N805
        valid_statistics = ["SampleCount", "Average", "Sum", "Minimum", "Maximum"]
        if statistic not in valid_statistics:
            raise ValueError(
                f"statistic: {statistic} is not valid. Valid statistics "
                "are: {valid_statistics}"
            )
        return statistic

    @validator("unit")
    def is_valid_unit(cls, unit: str) -> Union[str, None]:  # noqa: N805
        valid_units = [
            "Bits",
            "Bits/Second",
            "Bytes",
            "Bytes/Second",
            "Count",
            "Count/Second",
            "Gigabits",
            "Gigabits/Second",
            "Gigabytes",
            "Gigabytes/Second",
            "Kilobits",
            "Kilobits/Second",
            "Kilobytes",
            "Kilobytes/Second",
            "Megabits",
            "Megabits/Second",
            "Megabytes",
            "Megabytes/Second",
            "Microseconds",
            "Milliseconds",
            "Percent",
            "Seconds",
            "Terabits",
            "Terabits/Second",
            "Terabytes",
            "Terabytes/Second",
        ]
        if unit and unit.title() in valid_units:
            return unit.title()
        elif unit and unit.title() not in valid_units:
            raise ValueError(
                f"unit: {unit} is not valid. Valid units are: {valid_units}",
            )
        else:  # It is valid for a unit to not be provided.
            return None


class OLCloudWatchAlarmSimpleElastiCacheConfig(OLCloudWatchAlarmSimpleConfig):
    """Configuration object for definition monitoring of an ElastiCache
    deployment.
    """

    cluster_id: Union[str, Output[str]]
    node_id: str
    namespace: str = "AWS/ElastiCache"

    class Config:
        arbitrary_types_allowed = True


class OLCloudWatchAlarmSimpleElastiCache(ComponentResource):
    metric_alarm: cloudwatch.MetricAlarm = None

    def __init__(
        self,
        alarm_config: OLCloudWatchAlarmSimpleElastiCacheConfig,
        opts: Optional[ResourceOptions] = None,
    ):
        super().__init__(
            "ol:infrastructure.aws.cloudwatch.OLCloudWatchAlarmElastiCache",
            alarm_config.name,
            None,
            opts,
        )
        resource_options = ResourceOptions(parent=self).merge(opts)  # type: ignore

        sns_topic_arn = get_monitoring_sns_arn(alarm_config.level)
        cache_cluster_id = f"{alarm_config.cluster_id}{alarm_config.node_id}"

        self.metric_alarm = cloudwatch.MetricAlarm(
            f"{cache_cluster_id}-{alarm_config.metric_name}-simple-elasticache_alarm",
            actions_enabled=True,
            alarm_actions=[sns_topic_arn],
            alarm_description=alarm_config.description,
            comparison_operator=alarm_config.comparison_operator,
            datapoints_to_alarm=alarm_config.datapoints_to_alarm,
            dimensions={
                "CacheClusterId": cache_cluster_id,
            },
            evaluation_periods=alarm_config.evaluation_periods,
            metric_name=alarm_config.metric_name,
            name=f"simple-elasticache-{cache_cluster_id}-{alarm_config.metric_name}",
            namespace=alarm_config.namespace,
            period=alarm_config.period,
            statistic=alarm_config.statistic,
            treat_missing_data=alarm_config.treat_missing_data_as,
            threshold=alarm_config.threshold,
            unit=alarm_config.unit,
            opts=resource_options,
        )


class OLCloudWatchAlarmSimpleRDSConfig(OLCloudWatchAlarmSimpleConfig):
    """Configuration object for defining monitoring of an RDS deployment."""

    database_identifier: str
    namespace: str = "AWS/RDS"


class OLCloudWatchAlarmSimpleRDS(ComponentResource):
    metric_alarm: cloudwatch.MetricAlarm = None

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

        sns_topic_arn = get_monitoring_sns_arn(alarm_config.level)

        self.metric_alarm = cloudwatch.MetricAlarm(
            f"{alarm_config.database_identifier}-{alarm_config.metric_name}-simple-rds-alarm",
            actions_enabled=True,
            alarm_actions=[sns_topic_arn],
            alarm_description=alarm_config.description,
            comparison_operator=alarm_config.comparison_operator,
            datapoints_to_alarm=alarm_config.datapoints_to_alarm,
            dimensions={
                "DBInstanceIdentifier": alarm_config.database_identifier,
            },
            evaluation_periods=alarm_config.evaluation_periods,
            metric_name=alarm_config.metric_name,
            name=f"simple-rds-{alarm_config.database_identifier}-{alarm_config.metric_name}",
            namespace=alarm_config.namespace,
            period=alarm_config.period,
            statistic=alarm_config.statistic,
            treat_missing_data=alarm_config.treat_missing_data_as,
            threshold=alarm_config.threshold,
            unit=alarm_config.unit,
            opts=resource_options,
        )
