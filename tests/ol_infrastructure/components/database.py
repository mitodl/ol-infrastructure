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
