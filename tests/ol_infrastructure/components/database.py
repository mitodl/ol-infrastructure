import pytest
from pydantic import ValidationError

from ol_infrastructure.components.aws.database import OLDBConfig

VALID_CONFIG = {
    "db_name": "testdb",
    "engine": "postgres",
    "engine_version": "12.2",
    "target_vpc_id": "testvpc",
    "tags": {"OU": "operations", "Environment": "test"},
}


def test_engine_validation():
    with pytest.raises(ValidationError):  # noqa: PT012
        bad_config = VALID_CONFIG.copy()
        bad_config["engine"] = "bad_engine"
        OLDBConfig(**bad_config)


def test_engine_version_validation():
    with pytest.raises(ValidationError):  # noqa: PT012
        bad_config = VALID_CONFIG.copy()
        bad_config["engine_version"] = "badversion"
        OLDBConfig(**bad_config)


def test_valid_config_validates():
    good_config = OLDBConfig(**VALID_CONFIG)
    assert good_config.engine == VALID_CONFIG["engine"]


def test_invalid_enhanced_monitoring_interval():
    with pytest.raises(ValidationError):  # noqa: PT012
        bad_config = VALID_CONFIG.copy()
        bad_config["enhanced_monitoring_interval"] = 7  # not a valid interval
        OLDBConfig(**bad_config)


def test_valid_enhanced_monitoring_interval_zero():
    config = VALID_CONFIG.copy()
    config["enhanced_monitoring_interval"] = 0
    good_config = OLDBConfig(**config)
    assert good_config.enhanced_monitoring_interval == 0


def test_valid_enhanced_monitoring_interval_nonzero():
    for interval in (1, 5, 10, 15, 30, 60):
        config = VALID_CONFIG.copy()
        config["enhanced_monitoring_interval"] = interval
        good_config = OLDBConfig(**config)
        assert good_config.enhanced_monitoring_interval == interval


def test_invalid_performance_insights_retention_period():
    with pytest.raises(ValidationError):  # noqa: PT012
        bad_config = VALID_CONFIG.copy()
        bad_config["performance_insights_retention_period"] = 14  # not 7 or 731
        OLDBConfig(**bad_config)


def test_valid_performance_insights_retention_periods():
    for days in (7, 731):
        config = VALID_CONFIG.copy()
        config["performance_insights_retention_period"] = days
        good_config = OLDBConfig(**config)
        assert good_config.performance_insights_retention_period == days
