import pytest
from ol_infrastructure.lib.ol_types import AWSBase
from pydantic import ValidationError

VALID_TAGS = {"OU": "operations", "Environment": "test"}


def test_tag_validation():
    with pytest.raises(ValueError):  # noqa: PT011
        AWSBase(tags={"foo": "bar", "Environment": "test"})
    with pytest.raises(ValueError):  # noqa: PT011
        AWSBase(tags={"foo": "bar", "OU": "test"})
    with pytest.raises(ValidationError):
        AWSBase(tags={"Environment": "test", "OU": "test"})


def test_region_validation():
    with pytest.raises(ValueError):  # noqa: PT011
        AWSBase(tags=VALID_TAGS, region="us-east-0")


def test_merged_tags():
    base_config = AWSBase(tags=VALID_TAGS)
    new_tags = base_config.merged_tags({"Foo": "bar"})
    assert new_tags == {
        "OU": "operations",
        "Environment": "test",
        "Foo": "bar",
    }


def test_pulumi_managed_tag():
    base_config = AWSBase(tags=VALID_TAGS)
    assert base_config.tags.pop("pulumi_managed") == "true"
