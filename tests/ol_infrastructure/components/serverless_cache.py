import pytest
from pydantic import ValidationError

from ol_infrastructure.components.aws.serverless_cache import OLAmazonServerlessCacheConfig

VALID_CONFIG = {
    "cache_name": "test-cache-name",
    "description": "Test serverless cache",
    "tags": {"OU": "operations", "Environment": "test"},
}


def test_cache_name_validation():
    """Test that cache name validation works correctly."""
    with pytest.raises(ValidationError):  # noqa: PT012
        bad_config = VALID_CONFIG.copy()
        bad_config["cache_name"] = "invalid_name_with_underscore"
        OLAmazonServerlessCacheConfig(**bad_config)


def test_cache_name_length_validation():
    """Test that cache name length validation works correctly."""
    with pytest.raises(ValidationError):  # noqa: PT012
        bad_config = VALID_CONFIG.copy()
        bad_config["cache_name"] = "a" * 50  # Too long
        OLAmazonServerlessCacheConfig(**bad_config)


def test_cache_name_hyphen_validation():
    """Test that cache names cannot start or end with hyphens."""
    with pytest.raises(ValidationError):  # noqa: PT012
        bad_config = VALID_CONFIG.copy()
        bad_config["cache_name"] = "-invalid-name"
        OLAmazonServerlessCacheConfig(**bad_config)

    with pytest.raises(ValidationError):  # noqa: PT012
        bad_config = VALID_CONFIG.copy()
        bad_config["cache_name"] = "invalid-name-"
        OLAmazonServerlessCacheConfig(**bad_config)


def test_valid_config_validates():
    """Test that a valid configuration validates successfully."""
    good_config = OLAmazonServerlessCacheConfig(**VALID_CONFIG)
    assert good_config.cache_name == VALID_CONFIG["cache_name"]
    assert good_config.engine == "valkey"
    assert good_config.major_engine_version == 8


def test_default_values():
    """Test that default values are set correctly."""
    config = OLAmazonServerlessCacheConfig(**VALID_CONFIG)
    assert config.engine == "valkey"
    assert config.major_engine_version == 8
    assert config.description == "Test serverless cache"
    assert config.kms_key_id is None
    assert config.user_group_id is None