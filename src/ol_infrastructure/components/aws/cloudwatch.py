from pulumi import ComponentResource, Output, ResourceOptions
from pulumi_aws import cloudwatch
from pydantic import BaseModel, ConfigDict, PositiveInt, field_validator

from ol_infrastructure.lib.aws.monitoring_helper import get_monitoring_sns_arn


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
    period: PositiveInt = 300  # Five minutes
    statistic: str = "Average"
    threshold: int | float
    treat_missing_data_as: str = "missing"
    unit: str | None = None

    # TODO: MD 20230315 refactor to accomodate anomaly detection alerts  # noqa: E501, FIX002, TD002
    @field_validator("comparison_operator")
    @classmethod
    def is_valid_comparison_operator(
        cls,
        comparison_operator: str,
    ) -> str:
        valid_basic_operators = (
            "GreaterThanOrEqualToThreshold",
            "GreaterThanThreshold",
            "LessThanThreshold",
            "LessThanOrEqualToThreshold",
        )
        if comparison_operator not in valid_basic_operators:
            msg = f"comparison_operator: {comparison_operator} is not valid. Valid operators are: {{valid_basic_operators}}"  # noqa: E501
            raise ValueError(msg)
        return comparison_operator

    @field_validator("level")
    @classmethod
    def is_valid_level(cls, level: str) -> str:
        if level.lower() not in ("warning", "critical"):
            msg = f"level: {level} is not valid. Valid levels are 'warning' and 'critical'"  # noqa: E501
            raise ValueError(msg)
        return level.lower()

    @field_validator("treat_missing_data_as")
    @classmethod
    def is_valid_missing_data_as(
        cls,
        treat_missing_data_as: str,
    ) -> str:
        valid_treat_missing_data_as = ("missing", "ignore", "breaching", "notBreaching")
        if treat_missing_data_as not in valid_treat_missing_data_as:
            msg = f"treat_missing_data_as: {treat_missing_data_as} is not valid. Valid settings are: {{valid_treat_missing_data_as}}"  # noqa: E501
            raise ValueError(msg)
        return treat_missing_data_as

    @field_validator("statistic")
    @classmethod
    def is_valid_statistic(cls, statistic: str) -> str:
        valid_statistics = ("SampleCount", "Average", "Sum", "Minimum", "Maximum")
        if statistic not in valid_statistics:
            msg = f"statistic: {statistic} is not valid. Valid statistics are: {{valid_statistics}}"  # noqa: E501
            raise ValueError(msg)
        return statistic

    @field_validator("unit")
    @classmethod
    def is_valid_unit(cls, unit: str) -> str | None:
        valid_units = (
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
        )
        if unit and unit.title() in valid_units:
            return unit.title()
        elif unit and unit.title() not in valid_units:
            msg = f"unit: {unit} is not valid. Valid units are: {valid_units}"
            raise ValueError(
                msg,
            )
        else:  # It is valid for a unit to not be provided.
            return None


class OLCloudWatchAlarmSimpleElastiCacheConfig(OLCloudWatchAlarmSimpleConfig):
    """Configuration object for definition monitoring of an ElastiCache
    deployment.
    """

    cluster_id: str | Output[str]
    node_id: str
    namespace: str = "AWS/ElastiCache"
    tags: dict[str, str] | None = None
    model_config = ConfigDict(arbitrary_types_allowed=True)


class OLCloudWatchAlarmSimpleElastiCache(ComponentResource):
    metric_alarm: cloudwatch.MetricAlarm = None

    def __init__(
        self,
        alarm_config: OLCloudWatchAlarmSimpleElastiCacheConfig,
        opts: ResourceOptions | None = None,
    ):
        super().__init__(
            "ol:infrastructure.aws.cloudwatch.OLCloudWatchAlarmElastiCache",
            alarm_config.name,
            None,
            opts,
        )
        resource_options = ResourceOptions(parent=self).merge(opts)

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
            tags=alarm_config.tags,
            treat_missing_data=alarm_config.treat_missing_data_as,
            threshold=alarm_config.threshold,
            unit=alarm_config.unit,
            opts=resource_options,
        )


class OLCloudWatchAlarmSimpleRDSConfig(OLCloudWatchAlarmSimpleConfig):
    """Configuration object for defining monitoring of an RDS deployment."""

    database_identifier: str
    namespace: str = "AWS/RDS"
    tags: dict[str, str] | None = None


class OLCloudWatchAlarmSimpleRDS(ComponentResource):
    metric_alarm: cloudwatch.MetricAlarm = None

    def __init__(
        self,
        alarm_config: OLCloudWatchAlarmSimpleRDSConfig,
        opts: ResourceOptions | None = None,
    ):
        super().__init__(
            "ol:infrastructure.aws.cloudwatch.OLCloudWatchAlarmRDS",
            alarm_config.name,
            None,
            opts,
        )
        resource_options = ResourceOptions(parent=self).merge(opts)

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
            tags=alarm_config.tags,
            treat_missing_data=alarm_config.treat_missing_data_as,
            threshold=alarm_config.threshold,
            unit=alarm_config.unit,
            opts=resource_options,
        )
